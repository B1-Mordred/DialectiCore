from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity
from app.domain.schemas import (
    Approval,
    Asset,
    AuditEvent,
    Episode,
    ProductionManifestRequest,
    QualityResult,
    RenderPreset,
    RenderRequest,
    ThumbnailRequest,
    YouTubeExportRequest,
)
from app.services.object_storage import ObjectStore, create_object_store
from app.services.redaction import (
    is_sensitive_provider_response_key,
    safe_provider_response_payload,
)


class RenderService:
    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)

    def enqueue_render(
        self,
        episode: Episode,
        request: RenderRequest,
        presets: list[RenderPreset],
    ) -> Episode:
        """Persist a render request before a worker starts the expensive composition."""
        preset = self._preset_by_id(presets, request.preset_id)
        timeline_asset = self._target_timeline_asset(episode, request)
        is_talkshow_timeline = timeline_asset.source_entity_type == "transcript_version"
        if is_talkshow_timeline:
            self._ensure_timeline_integrity_passes(episode, timeline_asset)
        if (
            is_talkshow_timeline
            and request.render_type == "final"
            and not request.allow_unapproved_preview
        ):
            self._ensure_preview_approved(episode, timeline_asset)

        active = self._active_render_asset(episode, timeline_asset, request, preset)
        if active is not None:
            if active.status in {"submitted", "running"}:
                return episode
            if not request.regenerate:
                raise ValueError("render already exists for target timeline and preset")
            active.status = "replaced"
            active.updated_at = datetime.now(UTC)

        stage = (
            EpisodeStatus.rendering_preview
            if request.render_type == "preview"
            else EpisodeStatus.rendering_final
        )
        render_id = str(uuid4())
        now = datetime.now(UTC)
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=timeline_asset.language,
            source_entity_type="timeline_asset",
            source_entity_id=str(timeline_asset.id),
            status="submitted",
            generation_metadata={
                "adapter": "ffmpeg",
                "render_id": render_id,
                "render_type": request.render_type,
                "preset_id": preset.id,
                "timeline_asset_id": str(timeline_asset.id),
                "requested_at": now.isoformat(),
                "requested_by": request.user_id or "system",
                "render_request": self._render_request_payload(request),
            },
        )
        episode.assets.append(asset)
        episode.status = stage
        episode.updated_at = now
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="workflow.stage.changed",
                    actor=request.user_id or "system",
                    details={"stage": stage.value},
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type=f"render.{request.render_type}.requested",
                    actor=request.user_id or "system",
                    details={
                        "render_id": render_id,
                        "render_asset_id": str(asset.id),
                        "timeline_asset_id": str(timeline_asset.id),
                        "preset_id": preset.id,
                    },
                ),
            ]
        )
        return episode

    def start_queued_render(
        self,
        episode: Episode,
        render_asset_id: UUID,
        *,
        actor: str,
    ) -> Episode:
        asset = self._render_asset_by_id(episode, render_asset_id)
        if asset.status not in {"submitted", "running"}:
            raise ValueError("render request is not available for processing")
        now = datetime.now(UTC)
        asset.status = "running"
        asset.updated_at = now
        asset.generation_metadata["started_at"] = now.isoformat()
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type=f"render.{asset.generation_metadata.get('render_type')}.started",
                actor=actor,
                details={
                    "render_id": asset.generation_metadata.get("render_id"),
                    "render_asset_id": str(asset.id),
                },
            )
        )
        return episode

    def fail_queued_render(
        self,
        episode: Episode,
        render_asset_id: UUID,
        *,
        actor: str,
        error: str,
    ) -> Episode:
        asset = self._render_asset_by_id(episode, render_asset_id)
        now = datetime.now(UTC)
        asset.status = "failed"
        asset.updated_at = now
        asset.generation_metadata.update(
            {
                "failed_at": now.isoformat(),
                "failure_category": "render_failed",
                "failure_message": error,
            }
        )
        episode.status = EpisodeStatus.ready
        episode.updated_at = now
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type=f"render.{asset.generation_metadata.get('render_type')}.failed",
                    actor=actor,
                    details={
                        "render_id": asset.generation_metadata.get("render_id"),
                        "render_asset_id": str(asset.id),
                        "error": error,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="workflow.stage.changed",
                    actor=actor,
                    details={"stage": EpisodeStatus.ready.value},
                ),
            ]
        )
        return episode

    def render_episode(
        self,
        episode: Episode,
        request: RenderRequest,
        presets: list[RenderPreset],
        queued_render_asset_id: UUID | None = None,
    ) -> Episode:
        preset = self._preset_by_id(presets, request.preset_id)
        timeline_asset = self._target_timeline_asset(episode, request)
        is_talkshow_timeline = timeline_asset.source_entity_type == "transcript_version"
        if (
            is_talkshow_timeline
            and request.render_type == "final"
            and not request.allow_unapproved_preview
        ):
            self._ensure_preview_approved(episode, timeline_asset)
        timeline = self._timeline_json(timeline_asset)
        if is_talkshow_timeline:
            self._ensure_timeline_integrity_passes(episode, timeline_asset)
            self._ensure_studio_camera_renderable(episode, timeline)
        render_timeline = self._timeline_render_view(timeline)
        existing = self._latest_render_asset(episode, timeline_asset, request, preset)
        queued_asset = self._queued_render_asset(
            episode,
            queued_render_asset_id,
            timeline_asset,
            request,
            preset,
        )
        if existing is not None and not request.regenerate:
            raise ValueError("render already exists for target timeline and preset")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        stage = (
            EpisodeStatus.rendering_preview
            if request.render_type == "preview"
            else EpisodeStatus.rendering_final
        )
        episode.status = stage
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": stage.value},
            )
        )

        render_id = str(
            (
                queued_asset.generation_metadata.get("render_id")
                if queued_asset is not None
                else None
            )
            or uuid4()
        )
        manifest = self._render_manifest(
            episode=episode,
            timeline_asset=timeline_asset,
            timeline=timeline,
            preset=preset,
            request=request,
            render_id=render_id,
        )
        if isinstance(render_timeline.get("render_materialization"), dict):
            manifest["parallel_track_render_view"] = render_timeline["render_materialization"]
        manifest_payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        stored_manifest = self.object_store.put_bytes(
            key=f"renders/{episode.id}/{render_id}.manifest.json",
            payload=manifest_payload,
            content_type="application/vnd.dialecticore.render-manifest+json",
        )

        media_bytes = self._render_media_bytes(
            episode,
            render_timeline,
            preset,
            manifest,
            request.render_type,
        )
        stored_render = self.object_store.put_bytes(
            key=f"renders/{episode.id}/{render_id}.{preset.container}",
            payload=media_bytes,
            content_type="video/mp4" if preset.container == "mp4" else "application/octet-stream",
        )
        probe = self._probe_render(stored_render.path)
        asset = queued_asset or Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=timeline.get("language"),
            source_entity_type="timeline_asset",
            source_entity_id=str(timeline_asset.id),
        )
        asset.language = timeline.get("language")
        asset.storage_uri = stored_render.uri
        asset.mime_type = stored_render.content_type
        asset.duration_ms = probe.get("duration_ms")
        asset.width = probe.get("width")
        asset.height = probe.get("height")
        asset.fps = probe.get("fps")
        asset.checksum = stored_render.checksum
        asset.status = "completed"
        asset.updated_at = datetime.now(UTC)
        asset.generation_metadata.update(
            {
                "adapter": "ffmpeg",
                "render_id": render_id,
                "render_type": request.render_type,
                "preset_id": preset.id,
                "timeline_asset_id": str(timeline_asset.id),
                "timeline_id": timeline.get("id"),
                "render_manifest_uri": stored_manifest.uri,
                "render_manifest_checksum": stored_manifest.checksum,
                "render_manifest_path": str(stored_manifest.path),
                "object_storage_path": str(stored_render.path),
                "storage_backend": stored_render.backend,
                "render_manifest": manifest,
                "review_scope": request.review_scope,
                "composition_policy": "studio_camera_cuts.v1",
                "media_probe": probe,
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )
        caption_track = self._store_render_caption_track(
            episode=episode,
            timeline=timeline,
            render_asset=asset,
            render_id=render_id,
        )
        if caption_track is not None:
            asset.generation_metadata["caption_track_asset_id"] = str(caption_track.id)
            asset.generation_metadata["caption_track_mode"] = "selectable"
            asset.generation_metadata["caption_track_cue_count"] = int(
                caption_track.generation_metadata.get("cue_count") or 0
            )
            manifest["caption_track"] = {
                "asset_id": str(caption_track.id),
                "mode": "selectable",
                "cue_count": int(caption_track.generation_metadata.get("cue_count") or 0),
            }
            stored_manifest = self.object_store.put_bytes(
                key=f"renders/{episode.id}/{render_id}.manifest.json",
                payload=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
                content_type="application/vnd.dialecticore.render-manifest+json",
            )
            asset.generation_metadata.update(
                {
                    "render_manifest_uri": stored_manifest.uri,
                    "render_manifest_checksum": stored_manifest.checksum,
                    "render_manifest_path": str(stored_manifest.path),
                }
            )
        else:
            asset.generation_metadata["caption_track_mode"] = str(
                timeline.get("media", {}).get("subtitle_mode") or "off"
            )
        if queued_asset is None:
            episode.assets.append(asset)
        if caption_track is not None:
            episode.assets.append(caption_track)
        qc = self._render_qc(
            episode=episode,
            render_asset=asset,
            timeline=timeline,
            preset=preset,
            probe=probe,
        )
        episode.quality_results.append(qc)
        render_approval = None
        if is_talkshow_timeline and request.render_type == "preview":
            render_approval = Approval(
                episode_id=episode.id,
                stage="preview_render_review",
                target_type="render_asset",
                target_id=str(asset.id),
                comment="Preview render approval gates final rendering.",
            )
            episode.approvals.append(render_approval)
            asset.generation_metadata["approval_status"] = "pending"
            asset.generation_metadata["approval_id"] = str(render_approval.id)
        elif is_talkshow_timeline and request.render_type == "final":
            render_approval = Approval(
                episode_id=episode.id,
                stage="final_render_review",
                target_type="render_asset",
                target_id=str(asset.id),
                comment="Final render approval blocks delivery packaging.",
            )
            episode.approvals.append(render_approval)
            asset.generation_metadata["approval_status"] = "pending"
            asset.generation_metadata["approval_id"] = str(render_approval.id)
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="render.manifest.created",
                    actor=request.user_id or "system",
                    details={
                        "render_id": render_id,
                        "render_asset_id": str(asset.id),
                        "timeline_asset_id": str(timeline_asset.id),
                        "manifest_uri": stored_manifest.uri,
                        "checksum": stored_manifest.checksum,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type=f"render.{request.render_type}.completed",
                    actor=request.user_id or "system",
                    details={
                        "render_id": render_id,
                        "render_asset_id": str(asset.id),
                        "preset_id": preset.id,
                        "storage_uri": stored_render.uri,
                        "checksum": stored_render.checksum,
                        "duration_ms": asset.duration_ms,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="render.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "render_id": render_id,
                        "render_asset_id": str(asset.id),
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
        if render_approval is not None:
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="approval.required",
                    actor=request.user_id or "system",
                    details={
                        "approval_id": str(render_approval.id),
                        "stage": render_approval.stage,
                        "target_type": render_approval.target_type,
                        "target_id": render_approval.target_id,
                        "render_asset_id": str(asset.id),
                    },
                )
            )
        episode.status = EpisodeStatus.ready
        episode.updated_at = datetime.now(UTC)
        return episode

    @staticmethod
    def _compact_replaced_production_manifest_assets(episode: Episode) -> None:
        for asset in episode.assets:
            if asset.asset_type != AssetType.production_manifest or not isinstance(
                asset.generation_metadata, dict
            ):
                continue
            asset.status = "replaced"
            asset.updated_at = datetime.now(UTC)
            embedded = asset.generation_metadata.pop("production_manifest", None)
            if not isinstance(embedded, dict):
                continue
            asset.generation_metadata["production_manifest_summary"] = {
                "id": embedded.get("id"),
                "schema_version": embedded.get("schema_version"),
                "created_at": embedded.get("created_at"),
                "package_asset_id": asset.source_entity_id,
                "stored_immutably": bool(asset.storage_uri and asset.checksum),
            }

    def _timeline_render_view(self, timeline: dict) -> dict:
        """Materialize parallel B-roll only for the FFmpeg render boundary.

        The persisted v3 timeline retains one uninterrupted dialogue segment.
        Rendering may divide that interval at presentation boundaries, while
        carrying dialogue and source offsets forward deterministically.
        """
        tracks = timeline.get("tracks")
        if not isinstance(tracks, dict):
            return timeline
        content_clips = [clip for clip in tracks.get("broll_content", []) if isinstance(clip, dict)]
        presentation_clips = [
            clip for clip in tracks.get("broll_presentation", []) if isinstance(clip, dict)
        ]
        if not content_clips or not presentation_clips:
            return timeline
        content_by_id = {str(clip.get("id")): clip for clip in content_clips if clip.get("id")}
        render_segments: list[dict] = []
        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            segment_start = int(segment.get("start_ms") or 0)
            segment_end = int(segment.get("end_ms") or 0)
            overlapping = [
                clip
                for clip in presentation_clips
                if int(clip.get("start_ms") or 0) < segment_end
                and int(clip.get("end_ms") or 0) > segment_start
                and str(clip.get("linked_segment_id") or "") in {"", str(segment.get("id") or "")}
            ]
            if not overlapping:
                render_segments.append(segment)
                continue
            boundaries = {segment_start, segment_end}
            for clip in overlapping:
                boundaries.add(max(segment_start, int(clip.get("start_ms") or 0)))
                boundaries.add(min(segment_end, int(clip.get("end_ms") or 0)))
                for keyframe in clip.get("keyframes", []):
                    if isinstance(keyframe, dict):
                        time_ms = int(keyframe.get("time_ms") or 0)
                        if segment_start < time_ms < segment_end:
                            boundaries.add(time_ms)
            ordered = sorted(boundaries)
            for piece_index, (piece_start, piece_end) in enumerate(
                zip(ordered, ordered[1:], strict=False), start=1
            ):
                if piece_end <= piece_start:
                    continue
                active = next(
                    (
                        clip
                        for clip in overlapping
                        if int(clip.get("start_ms") or 0) <= piece_start
                        and int(clip.get("end_ms") or 0) >= piece_end
                    ),
                    None,
                )
                piece = self._timeline_render_piece(
                    segment=segment,
                    piece_index=piece_index,
                    start_ms=piece_start,
                    end_ms=piece_end,
                )
                if active is not None:
                    content = content_by_id.get(str(active.get("content_clip_id") or ""))
                    if content is not None:
                        piece = self._apply_parallel_broll_to_piece(
                            piece=piece,
                            presentation=active,
                            content=content,
                            piece_start_ms=piece_start,
                        )
                render_segments.append(piece)
        return {
            **timeline,
            "segments": render_segments,
            "render_materialization": {
                "schema_version": "dialecticore.parallel_track_render_view.v1",
                "source_segment_count": len(timeline.get("segments", [])),
                "render_segment_count": len(render_segments),
                "broll_content_clip_count": len(content_clips),
                "broll_presentation_clip_count": len(presentation_clips),
                "source_clock_preserved": True,
            },
        }

    def _timeline_render_piece(
        self,
        *,
        segment: dict,
        piece_index: int,
        start_ms: int,
        end_ms: int,
    ) -> dict:
        relative_start_ms = start_ms - int(segment.get("start_ms") or 0)
        source_base_ms = int(
            segment.get("source_start_ms") or segment.get("audio_source_offset_ms") or 0
        )
        return {
            **segment,
            "id": f"{segment.get('id')}-render-{piece_index:02d}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": end_ms - start_ms,
            "audio_source_offset_ms": int(segment.get("audio_source_offset_ms") or 0)
            + relative_start_ms,
            "source_start_ms": source_base_ms + relative_start_ms,
            "source_end_ms": source_base_ms + relative_start_ms + end_ms - start_ms,
            "graphics": segment.get("graphics", []) if relative_start_ms == 0 else [],
            "citations": segment.get("citations", []) if relative_start_ms == 0 else [],
            "citation_overlay_asset_ids": (
                segment.get("citation_overlay_asset_ids", []) if relative_start_ms == 0 else []
            ),
        }

    def _apply_parallel_broll_to_piece(
        self,
        *,
        piece: dict,
        presentation: dict,
        content: dict,
        piece_start_ms: int,
    ) -> dict:
        asset_id = str(content.get("asset_id") or "")
        if not asset_id:
            return piece
        state = "rear_screen"
        active_keyframe: dict | None = None
        for keyframe in sorted(
            (item for item in presentation.get("keyframes", []) if isinstance(item, dict)),
            key=lambda item: int(item.get("time_ms") or 0),
        ):
            if int(keyframe.get("time_ms") or 0) <= piece_start_ms:
                state = str(keyframe.get("state") or state)
                active_keyframe = keyframe
        transition_duration_ms = self._optional_int(
            (active_keyframe or {}).get("transition_duration_ms")
        )
        if transition_duration_ms is None:
            transition_duration_ms = self._optional_int(presentation.get("transition_duration_ms"))
        transition_duration_ms = max(0, min(5_000, transition_duration_ms or 0))
        content_source_start_ms = int(content.get("source_in_ms") or 0) + max(
            0, piece_start_ms - int(content.get("start_ms") or 0)
        )
        layer = {
            "role": "wall_screen_broll" if state == "rear_screen" else "broll",
            "asset_id": asset_id,
            "purpose": (
                "parallel_rear_screen_playback"
                if state == "rear_screen"
                else "parallel_fullscreen_playback"
            ),
            "source_start_ms": content_source_start_ms,
            "source_end_ms": content_source_start_ms + int(piece["duration_ms"]),
            "content_clip_id": content.get("id"),
            "presentation_clip_id": presentation.get("id"),
        }
        if state == "fullscreen":
            return {
                **piece,
                "segment_type": "discussion_parallel_broll_fullscreen",
                "video_asset_id": asset_id,
                "secondary_visual_asset_id": asset_id,
                "visual_role": "broll",
                "visual_layers": [layer],
                "camera_transition": "dissolve",
                "transition_duration_ms": transition_duration_ms,
                "direction": {
                    **(piece.get("direction") or {}),
                    "action": "dissolve",
                    "speaker_mouth_mode": "off_camera_dialogue",
                },
                "broll_playback": {
                    "state": state,
                    "content_clip_id": content.get("id"),
                    "presentation_clip_id": presentation.get("id"),
                    "source_start_ms": content_source_start_ms,
                },
            }
        base_layers = [
            existing
            for existing in piece.get("visual_layers", [])
            if isinstance(existing, dict) and existing.get("role") != "wall_screen_broll"
        ]
        return {
            **piece,
            "segment_type": "discussion_parallel_broll_rear_screen",
            "secondary_visual_asset_id": asset_id,
            "wall_screen_visual_asset_id": asset_id,
            "visual_layers": [*base_layers, layer],
            "camera_transition": "broll_insert",
            "transition_duration_ms": transition_duration_ms,
            "broll_playback": {
                "state": state,
                "content_clip_id": content.get("id"),
                "presentation_clip_id": presentation.get("id"),
                "source_start_ms": content_source_start_ms,
            },
        }

    def _ensure_studio_camera_renderable(self, episode: Episode, timeline: dict) -> None:
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        composition_policy = str(timeline.get("media", {}).get("composition_policy") or "")
        if composition_policy == "seated_studio_panel.v1":
            missing_roles: list[dict] = []
            for segment in timeline.get("segments", []):
                if not isinstance(segment, dict) or segment.get("segment_type") == "topic_primer":
                    continue
                primary = asset_by_id.get(str(segment.get("video_asset_id") or ""))
                direction = segment.get("direction")
                mouth_mode = (
                    direction.get("speaker_mouth_mode") if isinstance(direction, dict) else None
                )
                if not self._asset_path_exists(primary):
                    missing_roles.append(
                        {"segment_id": segment.get("id"), "role": "seated_panel_lipsync"}
                    )
                elif mouth_mode != "audio_driven_seated_panel":
                    missing_roles.append(
                        {"segment_id": segment.get("id"), "role": "audio_driven_seated_panel"}
                    )
                scene = asset_by_id.get(str(segment.get("studio_panel_scene_asset_id") or ""))
                if not self._asset_path_exists(scene):
                    missing_roles.append(
                        {"segment_id": segment.get("id"), "role": "studio_panel_keyframe"}
                    )
                wall_screen_asset_id = segment.get("wall_screen_visual_asset_id")
                if wall_screen_asset_id and not self._asset_path_exists(
                    asset_by_id.get(str(wall_screen_asset_id))
                ):
                    missing_roles.append(
                        {"segment_id": segment.get("id"), "role": "wall_screen_broll"}
                    )
            if missing_roles:
                summary = ", ".join(
                    f"{item['role']} for {item['segment_id']}" for item in missing_roles[:5]
                )
                raise ValueError(
                    "seated-studio render is blocked until B1 panel media is completed: " + summary
                )
            return
        directing = timeline.get("media", {}).get("directing", {})
        requires_generated_studio = bool(directing.get("require_generated_studio", False))
        discussion_segments = [
            segment
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict) and segment.get("segment_type") != "topic_primer"
        ]
        if not discussion_segments:
            return
        missing_roles: list[dict] = []
        for segment in discussion_segments:
            direction = segment.get("direction", {})
            requirements = direction.get("requirements", {}) if isinstance(direction, dict) else {}
            required_assets = [
                (
                    "studio_scene",
                    segment.get("studio_scene_asset_id"),
                    bool(requirements.get("studio_scene", requires_generated_studio)),
                ),
                (
                    "reaction_loop",
                    segment.get("reaction_visual_asset_id"),
                    bool(requirements.get("reaction_loop", False)),
                ),
                (
                    "studio_group_cutaway",
                    segment.get("studio_group_cutaway_asset_id"),
                    bool(requirements.get("studio_group_cutaway", False)),
                ),
            ]
            for role, asset_id, required in required_assets:
                if not required:
                    continue
                asset = asset_by_id.get(str(asset_id or ""))
                if not self._asset_path_exists(asset):
                    missing_roles.append(
                        {
                            "segment_id": segment.get("id"),
                            "role": role,
                            "asset_id": asset_id,
                        }
                    )
        if missing_roles:
            summary = ", ".join(
                f"{item['role']} for {item['segment_id']}" for item in missing_roles[:5]
            )
            raise ValueError(
                "studio-directed render is blocked until required media is completed: " + summary
            )

    @staticmethod
    def _ensure_timeline_integrity_passes(episode: Episode, timeline_asset: Asset) -> None:
        timeline = timeline_asset.generation_metadata.get("timeline_json")
        media = timeline.get("media") if isinstance(timeline, dict) else None
        # Hand-authored legacy timelines predate the studio composition contract.
        # Generated studio timelines always carry this policy and must have a
        # passing integrity result before the renderer can consume them.
        if not isinstance(media, dict) or media.get("composition_policy") not in {
            "studio_camera_cuts.v1",
            "seated_studio_panel.v1",
        }:
            return
        result = next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "timeline_integrity"
                and result.target_id == str(timeline_asset.id)
            ),
            None,
        )
        if result is None:
            raise ValueError("timeline integrity QC is required before rendering")
        if result.severity == QualitySeverity.fail:
            issues = result.details.get("issues") if isinstance(result.details, dict) else []
            blocking = [
                str(issue.get("issue"))
                for issue in issues
                if isinstance(issue, dict) and issue.get("severity") == "fail"
            ]
            detail = ", ".join(blocking[:4]) or "timeline integrity did not pass"
            raise ValueError(f"timeline integrity blocks rendering: {detail}")

    def generate_thumbnail(
        self,
        episode: Episode,
        request: ThumbnailRequest,
    ) -> Episode:
        render_asset = self._target_render_asset(episode, request.render_asset_id)
        existing = self._latest_thumbnail_asset(episode, render_asset)
        if existing is not None and not request.regenerate:
            raise ValueError("thumbnail already exists for target render")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        render_path = self._path_for_asset(render_asset)
        thumbnail_id = str(uuid4())
        seek_seconds = self._thumbnail_seek_seconds(render_asset)
        thumbnail_bytes = self._thumbnail_bytes(
            render_path,
            seek_seconds=seek_seconds,
        )
        stored_thumbnail = self.object_store.put_bytes(
            key=f"thumbnails/{episode.id}/{thumbnail_id}.jpg",
            payload=thumbnail_bytes,
            content_type="image/jpeg",
        )
        probe = self._probe_render(stored_thumbnail.path)
        average_luma = self._thumbnail_average_luma(stored_thumbnail.path)
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.thumbnail,
            language=render_asset.language,
            source_entity_type="render_asset",
            source_entity_id=str(render_asset.id),
            storage_uri=stored_thumbnail.uri,
            mime_type=stored_thumbnail.content_type,
            width=probe.get("width"),
            height=probe.get("height"),
            checksum=stored_thumbnail.checksum,
            status="completed",
            generation_metadata={
                "adapter": "ffmpeg",
                "thumbnail_id": thumbnail_id,
                "render_asset_id": str(render_asset.id),
                "render_id": render_asset.generation_metadata.get("render_id"),
                "object_storage_path": str(stored_thumbnail.path),
                "storage_backend": stored_thumbnail.backend,
                "media_probe": probe,
                "frame_seek_seconds": seek_seconds,
                "average_luma": average_luma,
            },
        )
        episode.assets.append(asset)
        qc = self._thumbnail_qc(
            episode,
            asset,
            render_asset,
            probe,
            average_luma=average_luma,
        )
        episode.quality_results.append(qc)
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="thumbnail.generated",
                    actor=request.user_id or "system",
                    details={
                        "thumbnail_asset_id": str(asset.id),
                        "thumbnail_id": thumbnail_id,
                        "render_asset_id": str(render_asset.id),
                        "storage_uri": stored_thumbnail.uri,
                        "checksum": stored_thumbnail.checksum,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="thumbnail.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "thumbnail_asset_id": str(asset.id),
                        "status": qc.status,
                        "failure_count": qc.details["failure_count"],
                        "warning_count": qc.details["warning_count"],
                    },
                ),
            ]
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def export_youtube_package(
        self,
        episode: Episode,
        request: YouTubeExportRequest,
    ) -> Episode:
        render_asset = self._target_render_asset(
            episode,
            request.render_asset_id,
            prefer_final=True,
            allow_preview=request.allow_preview_render,
        )
        if (
            render_asset.generation_metadata.get("render_type") != "final"
            and not request.allow_preview_render
        ):
            raise ValueError("YouTube export requires a final render")
        if (
            render_asset.generation_metadata.get("render_type") == "final"
            and not request.allow_preview_render
            and not self._final_render_approved(episode, render_asset)
        ):
            raise ValueError("final render must be approved before YouTube export")
        thumbnail_asset = self._target_thumbnail_asset(
            episode,
            request.thumbnail_asset_id,
            render_asset,
        )
        existing = self._latest_export_package_asset(episode, render_asset)
        if existing is not None and not request.regenerate:
            raise ValueError("YouTube package already exists for target render")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        package_id = str(uuid4())
        manifest = self._youtube_package_manifest(
            episode=episode,
            render_asset=render_asset,
            thumbnail_asset=thumbnail_asset,
            package_id=package_id,
        )
        package_bytes, included_files = self._youtube_package_bytes(
            episode=episode,
            manifest=manifest,
            render_asset=render_asset,
            thumbnail_asset=thumbnail_asset,
        )
        stored_package = self.object_store.put_bytes(
            key=f"exports/{episode.id}/{package_id}.youtube.zip",
            payload=package_bytes,
            content_type="application/zip",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language=render_asset.language,
            source_entity_type="render_asset",
            source_entity_id=str(render_asset.id),
            storage_uri=stored_package.uri,
            mime_type=stored_package.content_type,
            checksum=stored_package.checksum,
            status="completed",
            generation_metadata={
                "adapter": "youtube_exporter",
                "package_id": package_id,
                "render_asset_id": str(render_asset.id),
                "thumbnail_asset_id": str(thumbnail_asset.id) if thumbnail_asset else None,
                "object_storage_path": str(stored_package.path),
                "storage_backend": stored_package.backend,
                "youtube_package_manifest": manifest,
                "included_files": included_files,
            },
        )
        episode.assets.append(asset)
        qc = self._youtube_package_qc(
            episode=episode,
            package_asset=asset,
            render_asset=render_asset,
            thumbnail_asset=thumbnail_asset,
            included_files=included_files,
        )
        episode.quality_results.append(qc)
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="youtube.package.exported",
                    actor=request.user_id or "system",
                    details={
                        "package_asset_id": str(asset.id),
                        "package_id": package_id,
                        "render_asset_id": str(render_asset.id),
                        "thumbnail_asset_id": (
                            str(thumbnail_asset.id) if thumbnail_asset else None
                        ),
                        "included_files": included_files,
                        "storage_uri": stored_package.uri,
                        "checksum": stored_package.checksum,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="youtube.package.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "package_asset_id": str(asset.id),
                        "status": qc.status,
                        "failure_count": qc.details["failure_count"],
                        "warning_count": qc.details["warning_count"],
                    },
                ),
            ]
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def generate_production_manifest(
        self,
        episode: Episode,
        request: ProductionManifestRequest,
    ) -> Episode:
        package_asset = self._target_export_package_asset(episode, request.package_asset_id)
        self._ensure_package_qc_allows_manifest(episode, package_asset)
        render_asset = self._target_manifest_render_asset(
            episode,
            package_asset,
            request.render_asset_id,
        )
        existing = self._latest_production_manifest_asset(episode, package_asset)
        if existing is not None and not request.regenerate:
            raise ValueError("production manifest already exists for target package")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)
        self._compact_replaced_production_manifest_assets(episode)

        manifest_id = str(uuid4())
        manifest = self._production_manifest(
            episode=episode,
            manifest_id=manifest_id,
            package_asset=package_asset,
            render_asset=render_asset,
        )
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        stored = self.object_store.put_bytes(
            key=f"manifests/{episode.id}/{manifest_id}.production.json",
            payload=payload,
            content_type="application/vnd.dialecticore.production-manifest+json",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            language=render_asset.language or episode.source_language,
            source_entity_type="export_package",
            source_entity_id=str(package_asset.id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "adapter": "production_manifest",
                "manifest_id": manifest_id,
                "schema_version": manifest["schema_version"],
                "package_asset_id": str(package_asset.id),
                "render_asset_id": str(render_asset.id),
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
                "production_manifest": manifest,
                "asset_count": len(manifest["assets"]),
                "timeline_segment_count": len(manifest["timeline_segments"]),
                "quality_result_count": len(manifest["quality_results"]),
                "publish_job_count": len(manifest["publish_jobs"]),
            },
        )
        episode.assets.append(asset)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="production.manifest.created",
                actor=request.user_id or "system",
                details={
                    "manifest_asset_id": str(asset.id),
                    "manifest_id": manifest_id,
                    "package_asset_id": str(package_asset.id),
                    "render_asset_id": str(render_asset.id),
                    "storage_uri": stored.uri,
                    "checksum": stored.checksum,
                    "asset_count": len(manifest["assets"]),
                    "timeline_segment_count": len(manifest["timeline_segments"]),
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def _render_manifest(
        self,
        episode: Episode,
        timeline_asset: Asset,
        timeline: dict,
        preset: RenderPreset,
        request: RenderRequest,
        render_id: str,
    ) -> dict:
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        source_asset_ids: set[str] = {str(timeline_asset.id)}
        evidence_pack_asset = self._latest_evidence_pack_asset(episode)
        if evidence_pack_asset is not None:
            source_asset_ids.add(str(evidence_pack_asset.id))
        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            for key in (
                "audio_asset_id",
                "video_asset_id",
                "secondary_visual_asset_id",
                "reaction_visual_asset_id",
                "studio_scene_asset_id",
                "fallback_video_asset_id",
                "subtitle_asset_id",
            ):
                value = segment.get(key)
                if isinstance(value, str):
                    source_asset_ids.add(value)
            for layer in segment.get("visual_layers", []):
                if isinstance(layer, dict) and isinstance(layer.get("asset_id"), str):
                    source_asset_ids.add(layer["asset_id"])
            for value in segment.get("citation_overlay_asset_ids", []):
                if isinstance(value, str):
                    source_asset_ids.add(value)
        configured_studio_references = sorted(
            {
                str(segment.get("studio_reference_image_uri"))
                for segment in timeline.get("segments", [])
                if isinstance(segment, dict)
                and isinstance(segment.get("studio_reference_image_uri"), str)
                and segment.get("studio_reference_image_uri")
            }
        )
        source_assets = []
        for asset_id in sorted(source_asset_ids):
            asset = asset_by_id.get(asset_id)
            if asset is None:
                continue
            source_assets.append(
                {
                    "asset_id": str(asset.id),
                    "asset_type": asset.asset_type.value,
                    "source_entity_type": asset.source_entity_type,
                    "source_entity_id": asset.source_entity_id,
                    "status": asset.status,
                    "storage_uri": asset.storage_uri,
                    "mime_type": asset.mime_type,
                    "duration_ms": asset.duration_ms,
                    "width": asset.width,
                    "height": asset.height,
                    "fps": asset.fps,
                    "checksum": asset.checksum,
                    "render_ready": asset.generation_metadata.get("render_ready"),
                }
            )
        composition = self._scene_composition_plan(
            timeline=timeline,
            asset_by_id=asset_by_id,
            render_type=request.render_type,
            preset=preset,
        )
        return {
            "id": render_id,
            "schema_version": "render_manifest.v2",
            "episode_id": str(episode.id),
            "timeline_asset_id": str(timeline_asset.id),
            "timeline_id": timeline.get("id"),
            "render_type": request.render_type,
            "preset": preset.model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
            "timeline_duration_ms": timeline.get("duration_ms"),
            "source_assets": source_assets,
            "configured_studio_references": configured_studio_references,
            "evidence_lineage": self._evidence_lineage_for_timeline(
                timeline=timeline,
                evidence_pack_asset=evidence_pack_asset,
            ),
            "composition": composition,
            "composition_policy": "studio_camera_cuts.v1",
            "normalization": {
                "video": {
                    "width": preset.width,
                    "height": preset.height,
                    "fps": preset.fps,
                    "pixel_format": preset.pixel_format,
                    "codec": preset.codec,
                    "video_bitrate": preset.video_bitrate,
                    "timestamp_policy": "continuous_from_timeline_segments",
                },
                "audio": {
                    "sample_rate": preset.audio_sample_rate,
                    "layout": preset.audio_layout,
                    "audio_bitrate": preset.audio_bitrate,
                    "loudness_lufs": self.settings.audio_loudness_target_lufs,
                    "true_peak_dbtp": self.settings.audio_loudness_true_peak_limit_dbtp,
                    "silence_policy": "preserve_timeline_turn_durations",
                },
            },
            "composition_mode": (
                "timeline_scene_composite_preview"
                if request.render_type == "preview"
                else "timeline_scene_composite_final"
            ),
        }

    def _scene_composition_plan(
        self,
        timeline: dict,
        asset_by_id: dict[str, Asset],
        render_type: str,
        preset: RenderPreset,
    ) -> dict:
        segments = [
            segment for segment in timeline.get("segments", []) if isinstance(segment, dict)
        ]
        planned_segments = segments
        render_materialization = timeline.get("render_materialization")
        source_segment_count = (
            int(render_materialization.get("source_segment_count") or len(segments))
            if isinstance(render_materialization, dict)
            else len(segments)
        )
        visual_asset_ids = [
            asset_id
            for segment in planned_segments
            for asset_id in (
                segment.get("video_asset_id"),
                segment.get("secondary_visual_asset_id"),
                segment.get("reaction_visual_asset_id"),
                segment.get("studio_scene_asset_id"),
                segment.get("fallback_video_asset_id"),
            )
            if isinstance(asset_id, str)
        ]
        visual_asset_ids.extend(
            layer["asset_id"]
            for segment in planned_segments
            for layer in segment.get("visual_layers", [])
            if isinstance(layer, dict) and isinstance(layer.get("asset_id"), str)
        )
        visual_asset_ids = sorted(set(visual_asset_ids))
        audio_asset_ids = [
            str(segment.get("audio_asset_id"))
            for segment in planned_segments
            if isinstance(segment.get("audio_asset_id"), str)
        ]
        # Citations remain in evidence lineage for review. They are never
        # represented as synthetic overlays in a programme render.
        citation_overlay_ids: list[str] = []
        resolved_visual_asset_ids = [
            asset_id
            for asset_id in visual_asset_ids
            if self._asset_path_exists(asset_by_id.get(asset_id))
        ]
        resolved_audio_asset_ids = [
            asset_id
            for asset_id in audio_asset_ids
            if self._asset_path_exists(asset_by_id.get(asset_id))
        ]
        resolved_overlay_asset_ids = [
            asset_id
            for asset_id in citation_overlay_ids
            if self._asset_path_exists(asset_by_id.get(asset_id))
        ]
        subtitle_cache: dict[str, list[dict]] = {}
        segment_layers = [
            self._segment_composition_layer(
                segment,
                index,
                asset_by_id,
                subtitle_cache,
                preset,
            )
            for index, segment in enumerate(planned_segments, start=1)
        ]
        resolved_dialogue_audio_layer_count = sum(
            1 for layer in segment_layers if layer["dialogue_audio"].get("resolved")
        )
        subtitle_track_count = len(
            [layer for layer in segment_layers if layer["subtitle_track"].get("asset_id")]
        )
        resolved_subtitle_track_count = len(
            [layer for layer in segment_layers if layer["subtitle_track"].get("resolved")]
        )
        burned_in_caption_cue_count = (
            sum(len(layer["caption_cues"]) for layer in segment_layers)
            if timeline.get("media", {}).get("subtitle_mode") == "burned_in"
            else 0
        )
        visual_plate_layer_count = len(
            [layer for layer in segment_layers if layer["selected_visual"].get("asset_id")]
        )
        resolved_visual_plate_layer_count = len(
            [layer for layer in segment_layers if layer["selected_visual"].get("resolved")]
        )
        composited_visual_overlay_layer_count = sum(
            len(layer["composited_visual_overlays"]) for layer in segment_layers
        )
        rendered_layer_transform_names = sorted(
            {
                animation["rendered_transform"]
                for layer in segment_layers
                for visual_layer in layer.get("visual_layers", [])
                if isinstance(visual_layer, dict)
                and isinstance((animation := visual_layer.get("animation")), dict)
                and isinstance(animation.get("rendered_transform"), str)
            }
        )
        rendered_layer_transform_count = sum(
            1
            for layer in segment_layers
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict)
            and isinstance((animation := visual_layer.get("animation")), dict)
            and isinstance(animation.get("rendered_transform"), str)
        )
        rendered_layer_opacity_keyframe_count = sum(
            1
            for layer in segment_layers
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict)
            and isinstance((animation := visual_layer.get("animation")), dict)
            and animation.get("rendered_opacity_keyframes") is True
        )
        rendered_layer_scale_keyframe_count = sum(
            1
            for layer in segment_layers
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict)
            and isinstance((animation := visual_layer.get("animation")), dict)
            and animation.get("rendered_scale_keyframes") is True
        )
        rendered_layer_easing_curve_names = sorted(
            {
                easing
                for layer in segment_layers
                for visual_layer in layer.get("visual_layers", [])
                if isinstance(visual_layer, dict)
                and isinstance((animation := visual_layer.get("animation")), dict)
                and (
                    isinstance(animation.get("rendered_transform"), str)
                    or animation.get("rendered_scale_keyframes") is True
                )
                and isinstance((easing := animation.get("easing")), str)
                and easing not in {"linear", "none"}
            }
        )
        rendered_layer_easing_curve_count = sum(
            1
            for layer in segment_layers
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict)
            and isinstance((animation := visual_layer.get("animation")), dict)
            and (
                isinstance(animation.get("rendered_transform"), str)
                or animation.get("rendered_scale_keyframes") is True
            )
            and isinstance(animation.get("easing"), str)
            and animation["easing"] not in {"linear", "none"}
        )
        rendered_layer_mask_names = sorted(
            {
                mask["name"]
                for layer in segment_layers
                for visual_layer in layer.get("visual_layers", [])
                if isinstance(visual_layer, dict)
                and isinstance((slot := visual_layer.get("layout_slot")), dict)
                and isinstance((mask := slot.get("mask")), dict)
                and isinstance(mask.get("name"), str)
            }
        )
        rendered_layer_mask_count = sum(
            1
            for layer in segment_layers
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict)
            and isinstance((slot := visual_layer.get("layout_slot")), dict)
            and isinstance(slot.get("mask"), dict)
        )
        rendered_non_rectangular_mask_count = sum(
            1
            for layer in segment_layers
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict)
            and isinstance((slot := visual_layer.get("layout_slot")), dict)
            and isinstance((mask := slot.get("mask")), dict)
            and mask.get("name") in {"circle_alpha", "diamond_alpha"}
        )
        motion_primitive_names = sorted(
            {
                primitive["name"]
                for layer in segment_layers
                for primitive in layer.get("motion_primitives", [])
                if isinstance(primitive, dict) and isinstance(primitive.get("name"), str)
            }
        )
        advanced_layout_policy_count = sum(
            1
            for layer in segment_layers
            if isinstance(layer.get("layout_policy"), dict)
            and layer["layout_policy"].get("complexity") == "advanced"
        )
        split_screen_scene_count = sum(
            1
            for layer in segment_layers
            if isinstance(layer.get("layout_policy"), dict)
            and layer["layout_policy"].get("screen_mode") == "split_screen"
        )
        focus_shift_scene_count = sum(
            1
            for layer in segment_layers
            if isinstance(layer.get("layout_policy"), dict)
            and bool(layer["layout_policy"].get("focus_role"))
        )
        cross_scene_transition_count = sum(
            1
            for layer in segment_layers
            if isinstance(layer.get("transition_policy"), dict)
            and bool(layer["transition_policy"].get("cross_scene"))
        )
        rendered_cross_scene_xfade_count = 0
        layout_policy_names = sorted(
            {
                layer["layout_policy"]["name"]
                for layer in segment_layers
                if isinstance(layer.get("layout_policy"), dict)
            }
        )
        transition_policy_names = sorted(
            {
                layer["transition_policy"]["name"]
                for layer in segment_layers
                if isinstance(layer.get("transition_policy"), dict)
            }
        )
        animated_scene_count = sum(
            1
            for layer in segment_layers
            if isinstance(layer.get("transition_policy"), dict)
            and layer["transition_policy"].get("duration_ms", 0) > 0
        )
        studio_context_segment_count = sum(
            1 for layer in segment_layers if self._composition_layer_has_studio_context(layer)
        )
        post_primer_host_bridge_segment_count = sum(
            1
            for segment in planned_segments
            if segment.get("segment_type") == "post_primer_host_bridge"
        )
        return {
            "schema_version": "scene_composition.v2",
            "policy": "studio_camera_cuts.v1",
            "mode": f"timeline_scene_composite_{render_type}",
            "segment_count": source_segment_count,
            "source_segment_count": source_segment_count,
            "render_segment_count": len(planned_segments),
            "parallel_track_materialized": isinstance(render_materialization, dict),
            "generated_base_video": resolved_visual_plate_layer_count == 0,
            "generated_silent_audio": True,
            "visual_asset_count": len(visual_asset_ids),
            "resolved_visual_asset_count": len(resolved_visual_asset_ids),
            "visual_plate_layer_count": visual_plate_layer_count,
            "resolved_visual_plate_layer_count": resolved_visual_plate_layer_count,
            "generated_visual_fallback_count": len(segment_layers)
            - resolved_visual_plate_layer_count,
            "composited_visual_overlay_layer_count": composited_visual_overlay_layer_count,
            "audio_asset_count": len(audio_asset_ids),
            "resolved_audio_asset_count": len(resolved_audio_asset_ids),
            "dialogue_audio_layer_count": len(
                [layer for layer in segment_layers if layer["dialogue_audio"].get("asset_id")]
            ),
            "resolved_dialogue_audio_layer_count": resolved_dialogue_audio_layer_count,
            "silent_dialogue_fallback_count": len(segment_layers)
            - resolved_dialogue_audio_layer_count,
            "subtitle_track_count": subtitle_track_count,
            "resolved_subtitle_track_count": resolved_subtitle_track_count,
            "burned_in_caption_cue_count": burned_in_caption_cue_count,
            "citation_overlay_asset_count": len(citation_overlay_ids),
            "resolved_citation_overlay_asset_count": len(resolved_overlay_asset_ids),
            "composited_citation_overlay_count": 0,
            "layout_policy_names": layout_policy_names,
            "transition_policy_names": transition_policy_names,
            "animated_scene_count": animated_scene_count,
            "studio_context_segment_count": studio_context_segment_count,
            "post_primer_host_bridge_segment_count": post_primer_host_bridge_segment_count,
            "motion_primitive_names": motion_primitive_names,
            "motion_primitive_count": sum(
                len(layer.get("motion_primitives", [])) for layer in segment_layers
            ),
            "advanced_layout_policy_count": advanced_layout_policy_count,
            "split_screen_scene_count": split_screen_scene_count,
            "focus_shift_scene_count": focus_shift_scene_count,
            "cross_scene_transition_count": cross_scene_transition_count,
            "rendered_cross_scene_xfade_count": rendered_cross_scene_xfade_count,
            "cross_scene_renderer": "frame_scheduled_camera_cuts",
            "rendered_layer_transform_names": rendered_layer_transform_names,
            "rendered_layer_transform_count": rendered_layer_transform_count,
            "rendered_layer_opacity_keyframe_count": rendered_layer_opacity_keyframe_count,
            "rendered_layer_scale_keyframe_count": rendered_layer_scale_keyframe_count,
            "rendered_layer_easing_curve_names": rendered_layer_easing_curve_names,
            "rendered_layer_easing_curve_count": rendered_layer_easing_curve_count,
            "rendered_layer_mask_names": rendered_layer_mask_names,
            "rendered_layer_mask_count": rendered_layer_mask_count,
            "rendered_non_rectangular_mask_count": rendered_non_rectangular_mask_count,
            "layer_mask_renderer": (
                "ffmpeg_alpha_geometric_masks"
                if rendered_non_rectangular_mask_count
                else "ffmpeg_alpha_rounded_rect_masks"
                if rendered_layer_mask_count
                else "no_layer_masks"
            ),
            "layer_motion_renderer": (
                "ffmpeg_overlay_position_scale_opacity_eased_keyframes"
                if rendered_layer_easing_curve_count
                else "ffmpeg_overlay_position_scale_opacity_keyframes"
                if (
                    rendered_layer_transform_count
                    or rendered_layer_opacity_keyframe_count
                    or rendered_layer_scale_keyframe_count
                )
                else "static_overlay_geometry"
            ),
            "layout_policy_strategy": (
                "role_aware_studio_primary_broll_reaction_split_screen_focus_layouts"
            ),
            "transition_policy_strategy": (
                "timeline_camera_transition_to_cross_scene_motion_primitives"
            ),
            "audio_mix_strategy": ("timeline_ordered_dialogue_concatenation_with_silence_gaps"),
            "caption_composition_strategy": (
                "burned_in_by_explicit_policy"
                if timeline.get("media", {}).get("subtitle_mode") == "burned_in"
                else "render_timed_selectable_vtt_sidecar"
                if timeline.get("media", {}).get("subtitle_mode") == "selectable"
                else "captions_disabled"
            ),
            "video_composition_strategy": (
                "timeline_ordered_multilayer_visual_composition_with_generated_scene_fallbacks"
            ),
            "segment_scene_ids": [
                segment.get("id")
                for segment in planned_segments
                if isinstance(segment.get("id"), str)
            ],
            "segment_layers": segment_layers,
            "citation_overlay_asset_ids": [],
            "resolved_citation_overlay_asset_ids": resolved_overlay_asset_ids,
            "fallback_policy": (
                "Use the configured studio reference before generated deterministic "
                "backgrounds when B1 studio media is unavailable."
            ),
        }

    def _segment_composition_layer(
        self,
        segment: dict,
        index: int,
        asset_by_id: dict[str, Asset],
        subtitle_cache: dict[str, list[dict]],
        preset: RenderPreset,
    ) -> dict:
        primary_video = self._composition_asset_reference(
            segment.get("video_asset_id"),
            asset_by_id,
        )
        fallback_video = self._composition_asset_reference(
            segment.get("fallback_video_asset_id"),
            asset_by_id,
        )
        visual_layers = self._visual_layer_references(segment, asset_by_id)
        selected_visual = self._base_visual_reference(
            visual_layers=visual_layers,
            primary_video=primary_video,
            fallback_video=fallback_video,
        )
        composited_visual_overlays = [
            layer
            for layer in visual_layers
            if layer.get("resolved")
            and layer.get("asset_id") != selected_visual.get("asset_id")
            and layer.get("role") != "fallback"
            and layer.get("embedded_in_primary") is not True
        ]
        layout_policy = self._layout_policy_for_segment(
            segment=segment,
            visual_layers=visual_layers,
            selected_visual=selected_visual,
        )
        transition_policy = self._transition_policy_for_segment(segment, index)
        placed_visual_layers = self._placed_visual_layers(
            visual_layers=visual_layers,
            selected_visual=selected_visual,
            layout_policy=layout_policy,
            transition_policy=transition_policy,
            preset=preset,
        )
        placed_overlays = [
            layer
            for layer in placed_visual_layers
            if layer.get("resolved")
            and layer.get("asset_id") != selected_visual.get("asset_id")
            and layer.get("role") != "fallback"
            and layer.get("embedded_in_primary") is not True
        ]
        return {
            "scene_index": index,
            "scene_id": segment.get("id"),
            "start_ms": segment.get("start_ms"),
            "end_ms": segment.get("end_ms"),
            "duration_ms": segment.get("duration_ms"),
            "speaker_id": segment.get("speaker_id"),
            "source_turn_id": segment.get("source_turn_id"),
            "character_reference_image_uri": segment.get("character_reference_image_uri"),
            "character_reference_images": segment.get("character_reference_images", {}),
            "camera_transition": segment.get("camera_transition"),
            "visual_role": segment.get("visual_role"),
            "layout_policy": layout_policy,
            "transition_policy": transition_policy,
            "motion_primitives": self._scene_motion_primitives(
                layout_policy=layout_policy,
                transition_policy=transition_policy,
                visual_layers=placed_visual_layers,
            ),
            "dialogue_audio": self._composition_asset_reference(
                segment.get("audio_asset_id"),
                asset_by_id,
            ),
            "primary_video": primary_video,
            "secondary_visual": self._composition_asset_reference(
                segment.get("secondary_visual_asset_id"),
                asset_by_id,
            ),
            "reaction_visual": self._composition_asset_reference(
                segment.get("reaction_visual_asset_id"),
                asset_by_id,
            ),
            "studio_scene": self._composition_asset_reference(
                segment.get("studio_scene_asset_id"),
                asset_by_id,
            ),
            "fallback_video": fallback_video,
            "base_visual": selected_visual,
            "selected_visual": selected_visual,
            "visual_layers": placed_visual_layers,
            "composited_visual_overlays": placed_overlays,
            "legacy_composited_visual_overlays": composited_visual_overlays,
            "subtitle_track": self._composition_asset_reference(
                segment.get("subtitle_asset_id"),
                asset_by_id,
            ),
            "caption_cues": self._subtitle_cues_for_segment(
                segment,
                asset_by_id,
                subtitle_cache,
            ),
            "citation_overlays": [
                self._composition_asset_reference(overlay_id, asset_by_id)
                for overlay_id in segment.get("citation_overlay_asset_ids", [])
                if isinstance(overlay_id, str)
            ],
        }

    @staticmethod
    def _composition_layer_has_studio_context(layer: dict) -> bool:
        """Count both legacy studio plates and native seated-panel footage."""
        visual_layers = [
            visual_layer
            for visual_layer in layer.get("visual_layers", [])
            if isinstance(visual_layer, dict) and visual_layer.get("resolved")
        ]
        if any(visual_layer.get("role") == "studio_scene" for visual_layer in visual_layers):
            return True
        layout_policy = layer.get("layout_policy")
        layout_name = (
            str(layout_policy.get("name") or "") if isinstance(layout_policy, dict) else ""
        )
        return layout_name in {
            "seated_panel_full_frame",
            "seated_panel_virtual_camera",
            "seated_panel_rear_screen_cutaway",
        } and any(
            visual_layer.get("role") in {"video_primary", "studio_scene"}
            for visual_layer in visual_layers
        )

    def _layout_policy_for_segment(
        self,
        segment: dict,
        visual_layers: list[dict],
        selected_visual: dict,
    ) -> dict:
        roles = {str(layer.get("role") or "") for layer in visual_layers}
        selected_role = str(selected_visual.get("role") or "")
        direction = segment.get("direction")
        direction_view = direction.get("view") if isinstance(direction, dict) else None
        camera_view = str(direction_view or segment.get("camera_view") or "")
        if (
            isinstance(direction, dict)
            and direction.get("speaker_mouth_mode") == "audio_driven_seated_panel"
        ):
            rear_screen_cutaway = "wall_screen_broll" in roles
            virtual_camera = not rear_screen_cutaway and camera_view not in {
                "",
                "establishing_wide",
            }
            return {
                "name": (
                    "seated_panel_rear_screen_cutaway"
                    if rear_screen_cutaway
                    else "seated_panel_virtual_camera"
                    if virtual_camera
                    else "seated_panel_full_frame"
                ),
                "schema_version": "visual_layout_policy.v1",
                "complexity": "native_scene",
                "screen_mode": "full_frame",
                "focus_role": ("wall_screen_broll" if rear_screen_cutaway else "video_primary"),
                "composition_rules": [
                    (
                        "composite source-bound media inside the measured rear-screen safe region"
                        if rear_screen_cutaway
                        else "apply the configured virtual camera crop to the B1-generated "
                        "seated studio frame"
                        if virtual_camera
                        else "preserve the B1-generated seated studio frame unchanged"
                    ),
                    "do not cover the seated characters or desk",
                ],
                "virtual_camera": virtual_camera,
                "camera_view": camera_view or "establishing_wide",
                "safe_area": {"x": 48, "y": 38, "bottom": 50},
                "layer_order": (
                    ["studio_scene", "wall_screen_broll"]
                    if rear_screen_cutaway
                    else ["video_primary"]
                ),
            }
        if camera_view == "panel_two_shot" and {"studio_group_cutaway", "video_primary"} <= roles:
            name = "studio_group_speaker_focus"
            screen_mode = "speaker_with_panel_context"
            focus_role = "video_primary"
            complexity = "advanced"
        elif camera_view == "establishing_wide" and {"studio_scene", "video_primary"} <= roles:
            name = "studio_establishing_speaker_inset"
            screen_mode = "speaker_with_studio_context"
            focus_role = "studio_scene"
            complexity = "advanced"
        elif camera_view == "speaker_close_up" and {"studio_scene", "video_primary"} <= roles:
            name = "studio_speaker_close_up"
            screen_mode = "speaker_with_studio_context"
            focus_role = "video_primary"
            complexity = "advanced"
        elif camera_view == "speaker_medium" and {"studio_scene", "video_primary"} <= roles:
            name = "studio_speaker_medium"
            screen_mode = "speaker_with_studio_context"
            focus_role = "video_primary"
            complexity = "advanced"
        elif (
            camera_view == "reaction"
            and {"studio_scene", "video_primary", "reaction_loop"} <= roles
        ):
            name = "studio_split_screen_reaction_focus"
            screen_mode = "split_screen"
            focus_role = "reaction_loop"
            complexity = "advanced"
        elif {"studio_group_cutaway", "video_primary"} <= roles:
            name = "studio_group_speaker_focus"
            screen_mode = "speaker_with_panel_context"
            focus_role = "video_primary"
            complexity = "advanced"
        elif {"studio_scene", "video_primary", "broll", "reaction_loop"} <= roles:
            name = "studio_split_screen_broll_reaction_focus"
            screen_mode = "split_screen"
            focus_role = "video_primary"
            complexity = "advanced"
        elif {"studio_scene", "video_primary", "broll"} <= roles:
            name = "studio_primary_broll_focus"
            screen_mode = "focus_with_context"
            focus_role = "broll"
            complexity = "advanced"
        elif {"studio_scene", "video_primary", "reaction_loop"} <= roles:
            name = "studio_split_screen_reaction_focus"
            screen_mode = "split_screen"
            focus_role = "reaction_loop"
            complexity = "advanced"
        elif {"video_primary", "broll"} <= roles:
            name = "primary_broll_focus"
            screen_mode = "focus_with_context"
            focus_role = "broll"
            complexity = "advanced"
        elif {"video_primary", "reaction_loop"} <= roles:
            name = "primary_reaction_split_screen"
            screen_mode = "split_screen"
            focus_role = "reaction_loop"
            complexity = "advanced"
        elif selected_role == "broll" or segment.get("visual_role") == "broll":
            name = "full_frame_broll"
            screen_mode = "full_frame"
            focus_role = "broll"
            complexity = "basic"
        else:
            name = "full_frame_primary"
            screen_mode = "full_frame"
            focus_role = selected_role or "video_primary"
            complexity = "basic"
        return {
            "name": name,
            "schema_version": "visual_layout_policy.v1",
            "complexity": complexity,
            "screen_mode": screen_mode,
            "focus_role": focus_role,
            "composition_rules": [
                "preserve studio context when available",
                "keep active speaker readable in the primary focal region",
                "place evidence and B-roll away from subtitle safe areas",
                "reserve reaction media for secondary context or split-screen focus",
            ],
            "safe_area": {"x": 48, "y": 38, "bottom": 50},
            "layer_order": [
                "studio_group_cutaway",
                "studio_scene",
                "video_primary",
                "broll",
                "reaction_loop",
            ],
        }

    def _transition_policy_for_segment(self, segment: dict, index: int) -> dict:
        direction = segment.get("direction")
        direction_action = direction.get("action") if isinstance(direction, dict) else None
        camera_action = str(direction_action or segment.get("camera_action") or "")
        transition = (
            camera_action
            if camera_action not in {"", "cut"}
            else str(segment.get("camera_transition") or "cut")
        )
        policies = {
            "dissolve": {
                "name": "soft_dissolve",
                "type": "fade_in",
                "duration_ms": 300,
                "easing": "linear",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp"],
            },
            "studio_establishing": {
                "name": "studio_establishing_push",
                "type": "fade_in",
                "duration_ms": 500,
                "easing": "ease_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "background_push_in"],
            },
            "slow_push": {
                "name": "studio_establishing_push",
                "type": "fade_in",
                "duration_ms": 500,
                "easing": "ease_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "background_push_in"],
            },
            "slow_pull": {
                "name": "soft_dissolve",
                "type": "fade_in",
                "duration_ms": 300,
                "easing": "ease_in_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp"],
            },
            "fly_in": {
                "name": "studio_fly_in",
                "type": "fade_in",
                "duration_ms": 400,
                "easing": "ease_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "virtual_camera_zoom"],
            },
            "pan_left": {
                "name": "virtual_camera_pan_left",
                "type": "fade_in",
                "duration_ms": 250,
                "easing": "ease_in_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "virtual_camera_pan"],
            },
            "pan_right": {
                "name": "virtual_camera_pan_right",
                "type": "fade_in",
                "duration_ms": 250,
                "easing": "ease_in_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "virtual_camera_pan"],
            },
            "reaction_cutaway": {
                "name": "reaction_cutaway_snap",
                "type": "fade_in",
                "duration_ms": 180,
                "easing": "ease_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "reaction_focus_pop"],
            },
            "broll_insert": {
                "name": "broll_insert_slide",
                "type": "fade_in",
                "duration_ms": 240,
                "easing": "ease_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "broll_slide_in"],
            },
            "source_reveal": {
                "name": "source_reveal_arc",
                "type": "fade_in",
                "duration_ms": 420,
                "easing": "ease_in_out",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "broll_arc_reveal"],
            },
            "speaker_spotlight": {
                "name": "speaker_spotlight_bounce",
                "type": "fade_in",
                "duration_ms": 360,
                "easing": "ease_out_back",
                "cross_scene": True,
                "motion_primitives": ["opacity_ramp", "speaker_spotlight_bounce"],
            },
        }
        policy = policies.get(
            transition,
            {
                "name": "hard_cut",
                "type": "cut",
                "duration_ms": 0,
                "easing": "none",
                "cross_scene": False,
                "motion_primitives": [],
            },
        )
        terminal_fade_out_ms = self._optional_int(segment.get("terminal_fade_out_ms")) or 0
        source_range_timing_locked = bool(segment.get("source_range_timing_locked"))
        terminal_clip = bool(segment.get("terminal_clip"))
        transition_policy = {
            "schema_version": "visual_transition_policy.v1",
            "source_transition": transition,
            "scene_index": index,
            "applies_to": ["scene_plate", "visual_overlays"],
            "whole_scene_fade": segment.get("segment_type") != "discussion_wall_screen_insert",
            "terminal_fade_out_ms": max(0, terminal_fade_out_ms),
            **policy,
        }
        requested_duration_ms = self._optional_int(segment.get("transition_duration_ms"))
        if requested_duration_ms is not None and transition_policy["cross_scene"]:
            transition_policy["duration_ms"] = max(0, min(5_000, requested_duration_ms))
        if source_range_timing_locked or terminal_clip:
            transition_policy.update(
                {
                    "name": "timing_locked_source_cut",
                    "type": "cut",
                    "duration_ms": 0,
                    "easing": "none",
                    "cross_scene": False,
                    "motion_primitives": [],
                }
            )
        return transition_policy

    def _placed_visual_layers(
        self,
        visual_layers: list[dict],
        selected_visual: dict,
        layout_policy: dict,
        transition_policy: dict,
        preset: RenderPreset,
    ) -> list[dict]:
        placed = []
        for layer in visual_layers:
            role = str(layer.get("role") or "")
            animation = self._layer_animation_policy(
                role=role,
                transition_policy=transition_policy,
            )
            layout_slot = self._visual_layout_slot(
                role=role,
                selected=layer.get("asset_id") == selected_visual.get("asset_id"),
                layout_name=str(layout_policy.get("name") or ""),
                preset=preset,
            )
            layout_slot = self._layout_slot_with_animation_mask(layout_slot, animation)
            placed.append(
                {
                    **layer,
                    "layout_slot": layout_slot,
                    "animation": animation,
                }
            )
        return placed

    def _visual_layout_slot(
        self,
        role: str,
        selected: bool,
        layout_name: str,
        preset: RenderPreset,
    ) -> dict:
        if (
            layout_name
            in {
                "seated_panel_full_frame",
                "seated_panel_virtual_camera",
            }
            and role == "video_primary"
        ):
            return self._layout_slot(
                "seated_panel_full_frame", 0, 0, preset.width, preset.height, 0
            )
        if (
            layout_name
            in {
                "seated_panel_full_frame",
                "seated_panel_virtual_camera",
                "seated_panel_rear_screen_cutaway",
            }
            and role == "wall_screen_broll"
        ):
            return self._layout_slot(
                "seated_panel_rear_screen",
                int(preset.width * 0.18),
                int(preset.height * 0.19),
                int(preset.width * 0.50),
                int(preset.height * 0.23),
                20,
            )
        if role in {"studio_scene", "studio_group_cutaway"} or (
            selected and layout_name.startswith("full_frame")
        ):
            return self._layout_slot("full_frame", 0, 0, preset.width, preset.height, 0)
        if layout_name == "studio_group_speaker_focus" and role == "video_primary":
            return self._layout_slot(
                "speaker_over_panel_focus",
                48,
                int(preset.height * 0.12),
                int(preset.width * 0.56),
                int(preset.height * 0.76),
                20,
            )
        if layout_name == "studio_establishing_speaker_inset" and role == "video_primary":
            return self._layout_slot(
                "speaker_establishing_inset",
                48,
                int(preset.height * 0.30),
                int(preset.width * 0.34),
                int(preset.height * 0.50),
                20,
            )
        if layout_name == "studio_speaker_close_up" and role == "video_primary":
            return self._layout_slot(
                "speaker_close_up_focus",
                48,
                int(preset.height * 0.08),
                int(preset.width * 0.66),
                int(preset.height * 0.84),
                20,
            )
        if layout_name == "studio_speaker_medium" and role == "video_primary":
            return self._layout_slot(
                "speaker_medium_focus",
                48,
                int(preset.height * 0.14),
                int(preset.width * 0.52),
                int(preset.height * 0.72),
                20,
            )
        if layout_name in {
            "studio_split_screen_broll_reaction_focus",
            "studio_split_screen_reaction_focus",
            "primary_reaction_split_screen",
        }:
            if role == "video_primary":
                return self._layout_slot(
                    "primary_split_focus",
                    48,
                    int(preset.height * 0.14),
                    int(preset.width * 0.48),
                    int(preset.height * 0.64),
                    10,
                )
            if role == "reaction_loop":
                return self._layout_slot(
                    "reaction_split_focus",
                    int(preset.width * 0.54),
                    int(preset.height * 0.16),
                    int(preset.width * 0.40),
                    int(preset.height * 0.52),
                    20,
                )
        if role == "video_primary" and (
            layout_name.startswith("studio_pip") or layout_name.startswith("studio_primary")
        ):
            width = int(preset.width * 0.46)
            height = int(preset.height * 0.72)
            return self._layout_slot(
                "primary_inset",
                48,
                int(preset.height * 0.16),
                width,
                height,
                10,
            )
        if role == "broll":
            width = int(
                preset.width
                * (0.34 if layout_name == "studio_split_screen_broll_reaction_focus" else 0.38)
            )
            height = int(
                preset.height
                * (0.28 if layout_name == "studio_split_screen_broll_reaction_focus" else 0.34)
            )
            return self._layout_slot(
                "broll_context_panel"
                if layout_name in {"studio_primary_broll_focus", "primary_broll_focus"}
                else "broll_picture_in_picture",
                preset.width - width - 48,
                132,
                width,
                height,
                20,
            )
        if role == "reaction_loop":
            width = int(preset.width * 0.24)
            height = int(preset.height * 0.24)
            return self._layout_slot(
                "reaction_picture_in_picture",
                preset.width - width - 48,
                preset.height - height - 58,
                width,
                height,
                30,
            )
        return self._layout_slot(
            "supporting_picture_in_picture",
            int(preset.width * 0.65),
            int(preset.height * 0.62),
            int(preset.width * 0.30),
            int(preset.height * 0.30),
            40,
        )

    def _layout_slot(
        self,
        name: str,
        x: int,
        y: int,
        width: int,
        height: int,
        z_index: int,
    ) -> dict:
        return {
            "name": name,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "z_index": z_index,
            "opacity": 1.0,
            "mask": self._layout_mask_policy(width, height) if z_index > 0 else None,
        }

    def _layout_mask_policy(self, width: int, height: int) -> dict:
        radius = min(32, max(8, int(min(width, height) * 0.08)))
        return {
            "name": "rounded_rect_alpha",
            "radius_px": radius,
            "rendered_mask": "ffmpeg_alpha_mask",
        }

    def _layout_slot_with_animation_mask(self, layout_slot: dict, animation: dict) -> dict:
        mask_override = animation.get("mask_override")
        if not isinstance(mask_override, dict):
            return layout_slot
        if not isinstance(layout_slot.get("mask"), dict):
            return layout_slot
        return {**layout_slot, "mask": mask_override}

    def _layer_animation_policy(self, role: str, transition_policy: dict) -> dict:
        transition_name = str(transition_policy.get("name") or "hard_cut")
        if transition_name == "broll_insert_slide" and role in {
            "broll",
            "wall_screen_broll",
        }:
            return {
                "name": "slide_in_from_right",
                "duration_ms": 240,
                "easing": "ease_out",
                "motion_primitive": "broll_slide_in",
                "rendered_transform": "overlay_x_slide",
                "rendered_opacity_keyframes": True,
                "from": {"x_percent": 100, "opacity": 0.0},
                "to": {"x_percent": 0, "opacity": 1.0},
            }
        if transition_name == "reaction_cutaway_snap" and role == "reaction_loop":
            return {
                "name": "focus_pop_in",
                "duration_ms": 180,
                "easing": "ease_out",
                "motion_primitive": "reaction_focus_pop",
                "rendered_transform": "overlay_xy_focus_pop",
                "rendered_scale_keyframes": True,
                "rendered_opacity_keyframes": True,
                "from": {"scale": 1.04, "opacity": 0.0},
                "to": {"scale": 1.0, "opacity": 1.0},
            }
        if transition_name == "studio_establishing_push" and role == "video_primary":
            return {
                "name": "gentle_push_in",
                "duration_ms": 500,
                "easing": "ease_out",
                "motion_primitive": "background_push_in",
                "rendered_transform": "overlay_xy_push_in",
                "rendered_scale_keyframes": True,
                "rendered_opacity_keyframes": True,
                "from": {"scale": 1.03, "opacity": 0.0},
                "to": {"scale": 1.0, "opacity": 1.0},
            }
        if transition_name == "source_reveal_arc" and role == "broll":
            return {
                "name": "arc_reveal_from_lower_right",
                "duration_ms": 420,
                "easing": "ease_in_out",
                "motion_primitive": "broll_arc_reveal",
                "rendered_transform": "overlay_xy_arc_reveal",
                "rendered_scale_keyframes": True,
                "rendered_opacity_keyframes": True,
                "from": {"scale": 1.08, "opacity": 0.0},
                "to": {"scale": 1.0, "opacity": 1.0},
                "arc_height_percent": 18,
                "mask_override": {
                    "name": "diamond_alpha",
                    "rendered_mask": "ffmpeg_alpha_mask",
                    "feather_px": 3,
                },
            }
        if transition_name == "speaker_spotlight_bounce" and role == "video_primary":
            return {
                "name": "speaker_spotlight_bounce_in",
                "duration_ms": 360,
                "easing": "ease_out_back",
                "motion_primitive": "speaker_spotlight_bounce",
                "rendered_transform": "overlay_xy_spotlight_bounce",
                "rendered_scale_keyframes": True,
                "rendered_opacity_keyframes": True,
                "from": {"scale": 1.12, "opacity": 0.0},
                "to": {"scale": 1.0, "opacity": 1.0},
                "mask_override": {
                    "name": "circle_alpha",
                    "rendered_mask": "ffmpeg_alpha_mask",
                    "feather_px": 2,
                },
            }
        if int(transition_policy.get("duration_ms") or 0) > 0:
            return {
                "name": "scene_fade_in",
                "duration_ms": int(transition_policy.get("duration_ms") or 0),
                "easing": transition_policy.get("easing", "linear"),
                "motion_primitive": "opacity_ramp",
                "rendered_opacity_keyframes": True,
                "from": {"opacity": 0.0},
                "to": {"opacity": 1.0},
            }
        return {"name": "none", "duration_ms": 0}

    def _scene_motion_primitives(
        self,
        layout_policy: dict,
        transition_policy: dict,
        visual_layers: list[dict],
    ) -> list[dict]:
        primitives: list[dict] = []
        for primitive_name in transition_policy.get("motion_primitives", []):
            if isinstance(primitive_name, str):
                primitives.append(
                    {
                        "name": primitive_name,
                        "source": "transition_policy",
                        "duration_ms": transition_policy.get("duration_ms", 0),
                    }
                )
        if layout_policy.get("screen_mode") == "split_screen":
            primitives.append(
                {
                    "name": "split_screen_focus_layout",
                    "source": "layout_policy",
                    "focus_role": layout_policy.get("focus_role"),
                    "duration_ms": 0,
                }
            )
        for layer in visual_layers:
            animation = layer.get("animation", {})
            if not isinstance(animation, dict):
                continue
            primitive_name = animation.get("motion_primitive")
            if isinstance(primitive_name, str):
                primitives.append(
                    {
                        "name": primitive_name,
                        "source": "layer_animation",
                        "role": layer.get("role"),
                        "duration_ms": animation.get("duration_ms", 0),
                    }
                )
        deduped = []
        seen: set[tuple[object, object, object]] = set()
        for primitive in primitives:
            key = (primitive.get("name"), primitive.get("source"), primitive.get("role"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(primitive)
        return deduped

    def _composition_asset_reference(
        self,
        asset_id: object,
        asset_by_id: dict[str, Asset],
    ) -> dict:
        if not isinstance(asset_id, str):
            return {"asset_id": None, "resolved": False}
        asset = asset_by_id.get(asset_id)
        reference = {
            "asset_id": asset_id,
            "asset_type": asset.asset_type.value if asset else None,
            "storage_uri": asset.storage_uri if asset else None,
            "mime_type": asset.mime_type if asset else None,
            "duration_ms": asset.duration_ms if asset else None,
            "resolved": self._asset_path_exists(asset),
        }
        visual_role = asset.generation_metadata.get("visual_role") if asset else None
        if visual_role is not None:
            reference["visual_role"] = visual_role
        return reference

    def _visual_layer_references(
        self,
        segment: dict,
        asset_by_id: dict[str, Asset],
    ) -> list[dict]:
        layers: list[dict] = []
        raw_layers = segment.get("visual_layers")
        if isinstance(raw_layers, list):
            for layer in raw_layers:
                if not isinstance(layer, dict):
                    continue
                storage_uri = layer.get("storage_uri")
                if isinstance(storage_uri, str) and storage_uri:
                    direct_path = self._optional_path_for_storage_uri(storage_uri)
                    layers.append(
                        {
                            "asset_id": None,
                            "asset_type": layer.get("asset_type") or "image",
                            "storage_uri": storage_uri,
                            "mime_type": self._mime_type_for_storage_uri(storage_uri),
                            "duration_ms": None,
                            "resolved": direct_path is not None,
                            "role": layer.get("role"),
                            "purpose": layer.get("purpose"),
                            "reference_only": True,
                            "embedded_in_primary": layer.get("embedded_in_primary") is True,
                            "source_start_ms": layer.get("source_start_ms"),
                            "source_end_ms": layer.get("source_end_ms"),
                        }
                    )
                    continue
                reference = self._composition_asset_reference(
                    layer.get("asset_id"),
                    asset_by_id,
                )
                if not reference.get("asset_id"):
                    continue
                layers.append(
                    {
                        **reference,
                        "role": layer.get("role"),
                        "purpose": layer.get("purpose"),
                        "character_reference_image_uri": layer.get("character_reference_image_uri"),
                        "character_reference_images": layer.get("character_reference_images", {}),
                        "embedded_in_primary": layer.get("embedded_in_primary") is True,
                        "source_start_ms": layer.get("source_start_ms"),
                        "source_end_ms": layer.get("source_end_ms"),
                    }
                )
        if layers:
            return self._dedupe_visual_layers(layers)

        fallback_layers = []
        for role, key, purpose in (
            ("studio_group_cutaway", "studio_group_cutaway_asset_id", "silent_panel_cutaway"),
            ("studio_scene", "studio_scene_asset_id", "base"),
            ("video_primary", "video_asset_id", "talking_head"),
            ("broll", "secondary_visual_asset_id", "picture_in_picture"),
            ("reaction_loop", "reaction_visual_asset_id", "reaction_picture_in_picture"),
            ("fallback", "fallback_video_asset_id", "fallback"),
        ):
            reference = self._composition_asset_reference(segment.get(key), asset_by_id)
            if reference.get("asset_id"):
                fallback_layers.append(
                    {
                        **reference,
                        "role": role,
                        "purpose": purpose,
                    }
                )
        return self._dedupe_visual_layers(fallback_layers)

    def _base_visual_reference(
        self,
        visual_layers: list[dict],
        primary_video: dict,
        fallback_video: dict,
    ) -> dict:
        for preferred_role in (
            "studio_group_cutaway",
            "studio_scene",
            "video_primary",
            "fallback",
        ):
            match = next(
                (
                    layer
                    for layer in visual_layers
                    if layer.get("role") == preferred_role and layer.get("resolved")
                ),
                None,
            )
            if match is not None:
                return match
        match = next(
            (layer for layer in visual_layers if layer.get("resolved")),
            None,
        )
        if match is not None:
            return match
        return primary_video if primary_video.get("asset_id") else fallback_video

    def _dedupe_visual_layers(self, layers: list[dict]) -> list[dict]:
        deduped = []
        seen = set()
        for layer in layers:
            asset_id = layer.get("asset_id")
            storage_uri = layer.get("storage_uri")
            key = asset_id or storage_uri
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(layer)
        return deduped

    def _subtitle_cues_for_segment(
        self,
        segment: dict,
        asset_by_id: dict[str, Asset],
        subtitle_cache: dict[str, list[dict]],
    ) -> list[dict]:
        subtitle_asset_id = segment.get("subtitle_asset_id")
        if not isinstance(subtitle_asset_id, str):
            return []
        subtitle_asset = asset_by_id.get(subtitle_asset_id)
        if subtitle_asset is None:
            return []
        if subtitle_asset_id not in subtitle_cache:
            subtitle_cache[subtitle_asset_id] = self._parse_subtitle_asset_cues(subtitle_asset)
        segment_start_ms = int(segment.get("start_ms") or 0)
        segment_end_ms = int(segment.get("end_ms") or 0)
        if segment_end_ms <= segment_start_ms:
            return []
        cues = []
        for cue in subtitle_cache[subtitle_asset_id]:
            cue_start_ms = int(cue["start_ms"])
            cue_end_ms = int(cue["end_ms"])
            if cue_end_ms <= segment_start_ms or cue_start_ms >= segment_end_ms:
                continue
            cues.append(
                {
                    **cue,
                    "start_ms": max(cue_start_ms, segment_start_ms),
                    "end_ms": min(cue_end_ms, segment_end_ms),
                    "subtitle_asset_id": subtitle_asset_id,
                }
            )
        return cues

    def _parse_subtitle_asset_cues(self, subtitle_asset: Asset) -> list[dict]:
        subtitle_text = self._subtitle_text_for_asset(subtitle_asset)
        if not subtitle_text:
            return []
        blocks = subtitle_text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n")
        cues: list[dict] = []
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines or lines[0].upper() == "WEBVTT":
                continue
            timestamp_index = next(
                (index for index, line in enumerate(lines) if "-->" in line),
                None,
            )
            if timestamp_index is None:
                continue
            start_raw, end_raw = lines[timestamp_index].split("-->", 1)
            start_ms = self._subtitle_timestamp_ms(start_raw.strip())
            end_ms = self._subtitle_timestamp_ms(end_raw.strip().split()[0])
            if start_ms is None or end_ms is None or end_ms <= start_ms:
                continue
            index_value = None
            if timestamp_index > 0:
                index_value = self._optional_int(lines[timestamp_index - 1])
            text = self._caption_text(" ".join(lines[timestamp_index + 1 :]))
            if not text:
                continue
            cues.append(
                {
                    "index": index_value or len(cues) + 1,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text,
                }
            )
        return cues

    def _subtitle_text_for_asset(self, subtitle_asset: Asset) -> str:
        metadata_text = subtitle_asset.generation_metadata.get("subtitle_text")
        if isinstance(metadata_text, str) and metadata_text.strip():
            return metadata_text
        path = self._optional_path_for_asset(subtitle_asset)
        if path is None:
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _store_render_caption_track(
        self,
        episode: Episode,
        timeline: dict,
        render_asset: Asset,
        render_id: str,
    ) -> Asset | None:
        if timeline.get("media", {}).get("subtitle_mode") != "selectable":
            return None
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        source_cues: dict[str, list[dict]] = {}
        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            subtitle_asset_id = segment.get("subtitle_asset_id")
            if not isinstance(subtitle_asset_id, str) or subtitle_asset_id in source_cues:
                continue
            subtitle_asset = asset_by_id.get(subtitle_asset_id)
            if subtitle_asset is None:
                continue
            parsed = self._parse_subtitle_asset_cues(subtitle_asset)
            metadata_cues = subtitle_asset.generation_metadata.get("cues")
            if isinstance(metadata_cues, list):
                for index, cue in enumerate(parsed):
                    if index >= len(metadata_cues) or not isinstance(metadata_cues[index], dict):
                        continue
                    transcript_turn_id = metadata_cues[index].get("transcript_turn_id")
                    if isinstance(transcript_turn_id, str):
                        cue["transcript_turn_id"] = transcript_turn_id
            source_cues[subtitle_asset_id] = parsed

        segments_by_turn: dict[str, list[dict]] = {}
        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            source_turn_id = segment.get("source_turn_id")
            if isinstance(source_turn_id, str):
                segments_by_turn.setdefault(source_turn_id, []).append(segment)
        for segments in segments_by_turn.values():
            segments.sort(key=lambda segment: int(segment.get("start_ms") or 0))

        timed_cues: list[dict] = []
        for cues in source_cues.values():
            turn_start_ms: dict[str, int] = {}
            for cue in cues:
                turn_id = cue.get("transcript_turn_id")
                if isinstance(turn_id, str):
                    turn_start_ms.setdefault(turn_id, int(cue["start_ms"]))
            for cue in cues:
                turn_id = cue.get("transcript_turn_id")
                if not isinstance(turn_id, str) or turn_id not in segments_by_turn:
                    continue
                source_turn_start_ms = turn_start_ms.get(turn_id, int(cue["start_ms"]))
                cue_relative_start_ms = int(cue["start_ms"]) - source_turn_start_ms
                cue_relative_end_ms = int(cue["end_ms"]) - source_turn_start_ms
                for segment in segments_by_turn[turn_id]:
                    segment_offset_ms = int(segment.get("audio_source_offset_ms") or 0)
                    segment_duration_ms = int(segment.get("duration_ms") or 0)
                    segment_offset_end_ms = segment_offset_ms + segment_duration_ms
                    overlap_start_ms = max(cue_relative_start_ms, segment_offset_ms)
                    overlap_end_ms = min(cue_relative_end_ms, segment_offset_end_ms)
                    if overlap_end_ms <= overlap_start_ms:
                        continue
                    timed_cues.append(
                        {
                            "start_ms": int(segment.get("start_ms") or 0)
                            + overlap_start_ms
                            - segment_offset_ms,
                            "end_ms": int(segment.get("start_ms") or 0)
                            + overlap_end_ms
                            - segment_offset_ms,
                            "text": str(cue.get("text") or ""),
                            "source_turn_id": turn_id,
                        }
                    )
        if not timed_cues:
            # Older subtitle assets predate transcript-turn cue metadata. Their
            # cue clock starts at the first discussion segment, so retain that
            # deterministic mapping rather than silently dropping captions.
            for subtitle_asset_id, cues in source_cues.items():
                matching_segments = [
                    segment
                    for segment in timeline.get("segments", [])
                    if isinstance(segment, dict)
                    and segment.get("subtitle_asset_id") == subtitle_asset_id
                ]
                if not matching_segments:
                    continue
                offset_ms = min(int(segment.get("start_ms") or 0) for segment in matching_segments)
                for cue in cues:
                    timed_cues.append(
                        {
                            "start_ms": offset_ms + int(cue["start_ms"]),
                            "end_ms": offset_ms + int(cue["end_ms"]),
                            "text": str(cue.get("text") or ""),
                            "source_turn_id": None,
                        }
                    )
        if not timed_cues:
            return None
        timed_cues.sort(key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["text"]))
        lines = ["WEBVTT", ""]
        for index, cue in enumerate(timed_cues, start=1):
            lines.extend(
                [
                    str(index),
                    (
                        f"{self._vtt_timestamp(cue['start_ms'])} --> "
                        f"{self._vtt_timestamp(cue['end_ms'])}"
                    ),
                    self._caption_text(cue["text"]),
                    "",
                ]
            )
        subtitle_text = "\n".join(lines)
        stored = self.object_store.put_bytes(
            key=f"renders/{episode.id}/{render_id}.captions.vtt",
            payload=subtitle_text.encode("utf-8"),
            content_type="text/vtt",
        )
        return Asset(
            episode_id=episode.id,
            asset_type=AssetType.subtitle,
            language=timeline.get("language"),
            source_entity_type="render_asset",
            source_entity_id=str(render_asset.id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            duration_ms=max(int(cue["end_ms"]) for cue in timed_cues),
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "adapter": "render_timed_subtitle_composer",
                "format": "vtt",
                "subtitle_text": subtitle_text,
                "cue_count": len(timed_cues),
                "timeline_id": timeline.get("id"),
                "render_asset_id": str(render_asset.id),
                "timing_policy": "timeline_segment_audio_offsets.v1",
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
            },
        )

    @staticmethod
    def _vtt_timestamp(value_ms: int) -> str:
        hours, remainder = divmod(max(0, value_ms), 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1_000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    def _subtitle_timestamp_ms(self, value: str) -> int | None:
        timestamp = value.replace(",", ".")
        parts = timestamp.split(":")
        if len(parts) == 3:
            hours_raw, minutes_raw, seconds_raw = parts
        elif len(parts) == 2:
            hours_raw = "0"
            minutes_raw, seconds_raw = parts
        else:
            return None
        if "." in seconds_raw:
            seconds_text, milliseconds_text = seconds_raw.split(".", 1)
        else:
            seconds_text = seconds_raw
            milliseconds_text = "0"
        try:
            hours = int(hours_raw)
            minutes = int(minutes_raw)
            seconds = int(seconds_text)
            milliseconds = int(milliseconds_text[:3].ljust(3, "0"))
        except ValueError:
            return None
        return hours * 3_600_000 + minutes * 60_000 + seconds * 1_000 + milliseconds

    def _caption_text(self, value: str) -> str:
        return " ".join(value.replace("-->", "->").split())

    def _evidence_lineage_for_timeline(
        self,
        timeline: dict,
        evidence_pack_asset: Asset | None,
    ) -> dict:
        citations: list[dict] = []
        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            segment_id = segment.get("id")
            for citation in segment.get("citations", []):
                if not isinstance(citation, dict):
                    continue
                evidence_ref = citation.get("evidence_ref")
                if not isinstance(evidence_ref, str) or not evidence_ref:
                    continue
                citations.append(
                    {
                        "segment_id": segment_id,
                        "source_turn_id": citation.get("source_turn_id"),
                        "claim": citation.get("claim"),
                        "evidence_ref": evidence_ref,
                    }
                )

        referenced_source_ids = sorted({citation["evidence_ref"] for citation in citations})
        pack = self._evidence_pack_json(evidence_pack_asset) if evidence_pack_asset else {}
        source_by_id = {
            source["id"]: source
            for source in pack.get("source_index", [])
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        }
        referenced_sources = [
            {
                "source_id": source_id,
                "title": source.get("title"),
                "source_type": source.get("source_type"),
                "uri": source.get("uri"),
                "author": source.get("author"),
                "published_at": source.get("published_at"),
                "retrieved_at": source.get("retrieved_at"),
                "confidence": source.get("confidence"),
                "content_checksum": source.get("content_checksum"),
                "score_factors": source.get("score_factors", {}),
            }
            for source_id in referenced_source_ids
            if (source := source_by_id.get(source_id)) is not None
        ]
        unresolved_source_ids = [
            source_id for source_id in referenced_source_ids if source_id not in source_by_id
        ]
        citation_links = [
            {
                **citation,
                "source_title": source_by_id.get(citation["evidence_ref"], {}).get("title"),
                "source_uri": source_by_id.get(citation["evidence_ref"], {}).get("uri"),
            }
            for citation in citations
        ]
        retrieval_log = (
            evidence_pack_asset.generation_metadata.get("retrieval_tool_log", [])
            if evidence_pack_asset
            else []
        )
        return {
            "schema_version": "evidence_lineage.v1",
            "evidence_pack_asset_id": str(evidence_pack_asset.id) if evidence_pack_asset else None,
            "evidence_pack_id": pack.get("id"),
            "evidence_pack_checksum": evidence_pack_asset.checksum if evidence_pack_asset else None,
            "citation_count": len(citations),
            "referenced_source_ids": referenced_source_ids,
            "referenced_sources": referenced_sources,
            "unresolved_source_ids": unresolved_source_ids,
            "citation_links": citation_links,
            "retrieval_tool_log_summary": {
                "attempt_count": evidence_pack_asset.generation_metadata.get(
                    "retrieval_attempt_count",
                    0,
                )
                if evidence_pack_asset
                else 0,
                "success_count": evidence_pack_asset.generation_metadata.get(
                    "retrieval_success_count",
                    0,
                )
                if evidence_pack_asset
                else 0,
                "failure_count": evidence_pack_asset.generation_metadata.get(
                    "retrieval_failure_count",
                    0,
                )
                if evidence_pack_asset
                else 0,
                "entries": retrieval_log,
            },
        }

    def _render_media_bytes(
        self,
        episode: Episode,
        timeline: dict,
        preset: RenderPreset,
        manifest: dict,
        render_type: str,
    ) -> bytes:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise ValueError("ffmpeg is not available for rendering")
        duration_seconds = self._scheduled_video_duration_seconds(timeline, preset)
        with tempfile.TemporaryDirectory(prefix="dialecticore-render-") as directory:
            work_dir = Path(directory)
            output_path = Path(directory) / f"{manifest['id']}.{preset.container}"
            visual_path = self._visual_plate_path(
                episode=episode,
                timeline=timeline,
                preset=preset,
                duration_seconds=duration_seconds,
                directory=work_dir,
                ffmpeg=ffmpeg,
            )
            audio_path = self._dialogue_audio_mix_path(
                episode=episode,
                timeline=timeline,
                preset=preset,
                duration_seconds=duration_seconds,
                directory=work_dir,
                ffmpeg=ffmpeg,
            )
            command = [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(visual_path),
                "-i",
                str(audio_path),
                "-t",
                f"{duration_seconds:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                self._scene_video_filter(
                    episode=episode,
                    timeline=timeline,
                    preset=preset,
                    duration_seconds=duration_seconds,
                ),
                "-c:v",
                preset.codec,
                "-pix_fmt",
                preset.pixel_format,
                "-b:v",
                preset.video_bitrate,
                "-c:a",
                "aac",
                "-b:a",
                preset.audio_bitrate,
                "-movflags",
                "+faststart",
                str(output_path),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self._ffmpeg_timeout_seconds(duration_seconds, minimum=60),
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise ValueError(f"ffmpeg render failed: {exc}") from exc
            return output_path.read_bytes()

    def _visual_plate_path(
        self,
        episode: Episode,
        timeline: dict,
        preset: RenderPreset,
        duration_seconds: float,
        directory: Path,
        ffmpeg: str,
    ) -> Path:
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        segments = [
            segment
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict)
            and float(segment.get("start_ms") or 0) < duration_seconds * 1000
        ]
        pieces: list[dict] = []
        total_frames = max(1, round(duration_seconds * preset.fps))
        cursor_frame = 0
        for index, segment in enumerate(segments, start=1):
            start_seconds = max(0.0, float(segment.get("start_ms") or 0) / 1000)
            end_seconds = min(
                duration_seconds,
                float(segment.get("end_ms") or 0) / 1000,
            )
            if end_seconds <= start_seconds:
                continue
            transition_policy = self._transition_policy_for_segment(segment, index)
            start_frame = max(0, min(total_frames, round(start_seconds * preset.fps)))
            end_frame = max(start_frame, min(total_frames, round(end_seconds * preset.fps)))
            if start_frame > cursor_frame:
                gap_duration_seconds = (start_frame - cursor_frame) / preset.fps
                path = self._generated_visual_piece_path(
                    directory=directory,
                    filename=f"visual-gap-{index:04d}.mp4",
                    duration_seconds=gap_duration_seconds,
                    preset=preset,
                    ffmpeg=ffmpeg,
                    color="0x0f172a",
                    transition_policy=None,
                )
                pieces.append(
                    {
                        "path": path,
                        "duration_seconds": gap_duration_seconds,
                        "transition_policy": None,
                    }
                )
            piece_duration_seconds = end_seconds - start_seconds
            render_piece_duration_seconds = (end_frame - start_frame) / preset.fps
            if render_piece_duration_seconds <= 0:
                cursor_frame = end_frame
                continue
            visual_layers = self._resolved_visual_layers_for_segment(
                segment,
                asset_by_id,
                preset,
            )
            if len(visual_layers) > 1:
                path = self._layered_visual_piece_path(
                    layers=visual_layers,
                    directory=directory,
                    filename=f"visual-segment-{index:04d}.mp4",
                    duration_seconds=render_piece_duration_seconds,
                    preset=preset,
                    ffmpeg=ffmpeg,
                    transition_policy=transition_policy,
                )
            elif len(visual_layers) == 1:
                virtual_camera = self._seated_panel_virtual_camera(
                    segment=segment,
                    asset_by_id=asset_by_id,
                )
                path = self._normalized_visual_piece_path(
                    source_path=visual_layers[0]["path"],
                    asset=visual_layers[0]["asset"],
                    directory=directory,
                    filename=f"visual-segment-{index:04d}.mp4",
                    duration_seconds=render_piece_duration_seconds,
                    preset=preset,
                    ffmpeg=ffmpeg,
                    transition_policy=transition_policy,
                    source_start_ms=self._optional_int(segment.get("source_start_ms")) or 0,
                    source_end_ms=self._optional_int(segment.get("source_end_ms")),
                    still_motion=str(segment.get("still_motion") or "push_in"),
                    virtual_camera=virtual_camera,
                )
            else:
                path = self._generated_visual_piece_path(
                    directory=directory,
                    filename=f"visual-generated-{index:04d}.mp4",
                    duration_seconds=render_piece_duration_seconds,
                    preset=preset,
                    ffmpeg=ffmpeg,
                    color=self._scene_color(index - 1),
                    transition_policy=transition_policy,
                )
            pieces.append(
                {
                    "path": path,
                    "duration_seconds": render_piece_duration_seconds,
                    "transition_policy": transition_policy,
                    "timeline_duration_seconds": piece_duration_seconds,
                    "scheduled_start_frame": start_frame,
                    "scheduled_end_frame": end_frame,
                }
            )
            cursor_frame = end_frame

        if cursor_frame < total_frames:
            tail_duration_seconds = (total_frames - cursor_frame) / preset.fps
            path = self._generated_visual_piece_path(
                directory=directory,
                filename="visual-tail.mp4",
                duration_seconds=tail_duration_seconds,
                preset=preset,
                ffmpeg=ffmpeg,
                color="0x0f172a",
                transition_policy=None,
            )
            pieces.append(
                {
                    "path": path,
                    "duration_seconds": tail_duration_seconds,
                    "transition_policy": None,
                }
            )
        if not pieces:
            path = self._generated_visual_piece_path(
                directory=directory,
                filename="visual-empty.mp4",
                duration_seconds=duration_seconds,
                preset=preset,
                ffmpeg=ffmpeg,
                color="0x0f172a",
                transition_policy=None,
            )
            pieces.append(
                {
                    "path": path,
                    "duration_seconds": duration_seconds,
                    "transition_policy": None,
                }
            )
        return self._concat_video_pieces(
            pieces=pieces,
            directory=directory,
            ffmpeg=ffmpeg,
            preset=preset,
        )

    def _resolved_visual_layers_for_segment(
        self,
        segment: dict,
        asset_by_id: dict[str, Asset],
        preset: RenderPreset,
    ) -> list[dict]:
        references = self._visual_layer_references(segment, asset_by_id)
        layers = []
        for reference in references:
            if reference.get("embedded_in_primary") is True:
                continue
            asset_id = reference.get("asset_id")
            asset = asset_by_id.get(asset_id) if isinstance(asset_id, str) else None
            path = (
                self._optional_path_for_asset(asset)
                if asset is not None
                else self._optional_path_for_storage_uri(reference.get("storage_uri"))
            )
            if path is None or not self._visual_reference_supported(asset, reference, path):
                continue
            layers.append(
                {
                    "role": reference.get("role") or reference.get("visual_role"),
                    "purpose": reference.get("purpose"),
                    "asset": asset,
                    "path": path,
                    "storage_uri": reference.get("storage_uri"),
                    "mime_type": reference.get("mime_type"),
                    "layout_slot": reference.get("layout_slot"),
                    "source_start_ms": self._optional_int(reference.get("source_start_ms")) or 0,
                    "source_end_ms": self._optional_int(reference.get("source_end_ms")),
                }
            )
        if not layers:
            return []

        reference_layers = [
            {
                "asset_id": str(layer["asset"].id) if layer["asset"] is not None else None,
                "asset_type": (
                    layer["asset"].asset_type.value if layer["asset"] is not None else "image"
                ),
                "storage_uri": (
                    layer["asset"].storage_uri
                    if layer["asset"] is not None
                    else layer.get("storage_uri")
                ),
                "mime_type": (
                    layer["asset"].mime_type
                    if layer["asset"] is not None
                    else layer.get("mime_type") or self._mime_type_for_path(layer["path"])
                ),
                "duration_ms": layer["asset"].duration_ms if layer["asset"] is not None else None,
                "resolved": True,
                "role": layer.get("role"),
                "purpose": layer.get("purpose"),
            }
            for layer in layers
        ]
        selected_visual = self._base_visual_reference(
            visual_layers=reference_layers,
            primary_video=self._composition_asset_reference(
                segment.get("video_asset_id"),
                asset_by_id,
            ),
            fallback_video=self._composition_asset_reference(
                segment.get("fallback_video_asset_id"),
                asset_by_id,
            ),
        )
        layout_policy = self._layout_policy_for_segment(
            segment=segment,
            visual_layers=reference_layers,
            selected_visual=selected_visual,
        )
        transition_policy = self._transition_policy_for_segment(segment, 1)
        placed_layers = self._placed_visual_layers(
            visual_layers=reference_layers,
            selected_visual=selected_visual,
            layout_policy=layout_policy,
            transition_policy=transition_policy,
            preset=preset,
        )
        slot_by_asset_id = {
            str(layer.get("asset_id") or layer.get("storage_uri")): layer.get("layout_slot")
            for layer in placed_layers
            if layer.get("asset_id") or layer.get("storage_uri")
        }
        animation_by_asset_id = {
            str(layer.get("asset_id") or layer.get("storage_uri")): layer.get("animation")
            for layer in placed_layers
            if layer.get("asset_id") or layer.get("storage_uri")
        }
        for layer in layers:
            layer_key = str(
                layer["asset"].id
                if layer["asset"] is not None
                else layer.get("storage_uri") or layer["path"]
            )
            layer["layout_slot"] = slot_by_asset_id.get(layer_key)
            layer["animation"] = animation_by_asset_id.get(layer_key)

        base = next((layer for layer in layers if layer["role"] == "studio_group_cutaway"), None)
        if base is None:
            base = next((layer for layer in layers if layer["role"] == "studio_scene"), None)
        if base is None:
            base = next(
                (layer for layer in layers if layer["role"] == "video_primary"),
                None,
            )
        if base is None:
            base = next((layer for layer in layers if layer["role"] == "fallback"), None)
        if base is None:
            base = layers[0]

        ordered = [base]
        for preferred_role in ("video_primary", "broll", "reaction_loop", "fallback"):
            for layer in layers:
                if layer is not base and layer["role"] == preferred_role and layer not in ordered:
                    ordered.append(layer)
        for layer in layers:
            if base["role"] == "studio_group_cutaway" and layer["role"] == "studio_scene":
                continue
            if layer not in ordered:
                ordered.append(layer)
        return ordered

    def _selected_visual_asset(
        self,
        segment: dict,
        asset_by_id: dict[str, Asset],
    ) -> Asset | None:
        primary = asset_by_id.get(str(segment.get("video_asset_id") or ""))
        if self._asset_path_exists(primary):
            return primary
        fallback = asset_by_id.get(str(segment.get("fallback_video_asset_id") or ""))
        if self._asset_path_exists(fallback):
            return fallback
        return primary or fallback

    def _visual_asset_supported(self, asset: Asset | None) -> bool:
        if asset is None:
            return False
        mime_type = asset.mime_type or ""
        return mime_type in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "video/mp4",
            "video/webm",
        }

    def _visual_reference_supported(
        self,
        asset: Asset | None,
        reference: dict,
        path: Path,
    ) -> bool:
        if asset is not None:
            return self._visual_asset_supported(asset)
        mime_type = str(reference.get("mime_type") or self._mime_type_for_path(path))
        return mime_type in {"image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm"}

    def _normalized_visual_piece_path(
        self,
        source_path: Path,
        asset: Asset | None,
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
        transition_policy: dict | None,
        source_start_ms: int = 0,
        source_end_ms: int | None = None,
        still_motion: str = "push_in",
        virtual_camera: dict | None = None,
    ) -> Path:
        if (asset is not None and (asset.mime_type or "").startswith("image/")) or (
            asset is None and self._mime_type_for_path(source_path).startswith("image/")
        ):
            return self._image_visual_piece_path(
                source_path=source_path,
                directory=directory,
                filename=filename,
                duration_seconds=duration_seconds,
                preset=preset,
                ffmpeg=ffmpeg,
                transition_policy=transition_policy,
                still_motion=still_motion,
            )
        return self._video_visual_piece_path(
            source_path=source_path,
            directory=directory,
            filename=filename,
            duration_seconds=duration_seconds,
            preset=preset,
            ffmpeg=ffmpeg,
            transition_policy=transition_policy,
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            virtual_camera=virtual_camera,
        )

    def _layered_visual_piece_path(
        self,
        layers: list[dict],
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
        transition_policy: dict | None,
    ) -> Path:
        output_path = directory / filename
        command = [ffmpeg, "-hide_banner", "-y"]
        for layer in layers:
            asset = layer.get("asset")
            path = layer["path"]
            is_image = (asset is not None and (asset.mime_type or "").startswith("image/")) or (
                asset is None
                and str(layer.get("mime_type") or self._mime_type_for_path(path)).startswith(
                    "image/"
                )
            )
            if is_image:
                command.extend(["-loop", "1", "-i", str(path)])
            else:
                source_start_ms = int(layer.get("source_start_ms") or 0)
                command.extend(
                    [
                        "-stream_loop",
                        "-1",
                        "-ss",
                        f"{max(0, source_start_ms) / 1000:.3f}",
                        "-i",
                        str(path),
                    ]
                )

        filter_parts = [f"[0:v]{self._visual_normalization_filter(preset)}[v0]"]
        previous_label = "v0"
        for index, layer in enumerate(layers[1:], start=1):
            width, height, x, y = self._visual_overlay_geometry(
                str(layer.get("role") or ""),
                preset,
                layer.get("layout_slot"),
            )
            overlay_label = f"ov{index}"
            output_label = f"v{index}"
            filter_parts.append(
                self._animated_overlay_stream_filter(
                    input_index=index,
                    output_label=overlay_label,
                    width=width,
                    height=height,
                    preset=preset,
                    animation=layer.get("animation"),
                    layout_slot=layer.get("layout_slot"),
                )
            )
            overlay_x, overlay_y = self._animated_overlay_position(
                layer=layer,
                target_x=x,
                target_y=y,
                width=width,
                height=height,
                preset=preset,
            )
            filter_parts.append(
                f"[{previous_label}][{overlay_label}]"
                f"overlay=x={overlay_x}:y={overlay_y}:eval=frame:format=auto"
                f"[{output_label}]"
            )
            previous_label = output_label
        final_filter = self._append_transition_video_filter(
            f"format={preset.pixel_format}",
            transition_policy,
            duration_seconds,
        )
        filter_parts.append(f"[{previous_label}]{final_filter}[vout]")
        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[vout]",
                "-t",
                f"{max(duration_seconds, 0.001):.3f}",
                "-an",
                "-c:v",
                preset.codec,
                "-pix_fmt",
                preset.pixel_format,
                "-b:v",
                preset.video_bitrate,
                str(output_path),
            ]
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._ffmpeg_timeout_seconds(duration_seconds),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            roles = ", ".join(str(layer.get("role") or "unknown") for layer in layers)
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                detail = exc.stderr[-600:].strip()
            raise RuntimeError(
                f"layered visual composition failed for {filename} ({roles})"
                + (f": {detail}" if detail else "")
            ) from exc
        return output_path

    def _animated_overlay_stream_filter(
        self,
        input_index: int,
        output_label: str,
        width: int,
        height: int,
        preset: RenderPreset,
        animation: object,
        layout_slot: object = None,
    ) -> str:
        filters = [
            f"[{input_index}:v]scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
            f"fps={preset.fps}",
            "format=rgba",
        ]
        if isinstance(animation, dict) and animation.get("rendered_scale_keyframes") is True:
            filters.extend(
                self._overlay_scale_keyframe_filters(
                    width=width,
                    height=height,
                    preset=preset,
                    animation=animation,
                )
            )
        if isinstance(animation, dict) and animation.get("rendered_opacity_keyframes") is True:
            duration_seconds = self._animation_duration_seconds(animation)
            if duration_seconds > 0:
                filters.append(f"fade=t=in:st=0:d={duration_seconds:.3f}:alpha=1")
        filters.extend(self._overlay_mask_filters(layout_slot, width, height))
        # FFmpeg output labels attach directly to the final filter. Joining the
        # label as another comma-delimited filter produces an empty filter and
        # used to trigger the static-base fallback in the renderer.
        return ",".join(filters) + f"[{output_label}]"

    def _overlay_mask_filters(self, layout_slot: object, width: int, height: int) -> list[str]:
        if not isinstance(layout_slot, dict):
            return []
        mask = layout_slot.get("mask")
        if not isinstance(mask, dict):
            return []
        mask_name = mask.get("name")
        if mask_name == "circle_alpha":
            return self._circle_mask_filters(width, height)
        if mask_name == "diamond_alpha":
            return self._diamond_mask_filters()
        if mask_name != "rounded_rect_alpha":
            return []
        radius = self._optional_int(mask.get("radius_px"))
        if radius is None or radius <= 0:
            return []
        radius = min(radius, max(1, min(width, height) // 2))
        edge = radius + 1
        radius_squared = radius * radius
        alpha_expression = (
            f"if(lt(X,{radius})*lt(Y,{radius})"
            f"*gt(pow({radius}-X,2)+pow({radius}-Y,2),{radius_squared}),0,"
            f"if(gt(X,W-{edge})*lt(Y,{radius})"
            f"*gt(pow(X-(W-{edge}),2)+pow({radius}-Y,2),{radius_squared}),0,"
            f"if(lt(X,{radius})*gt(Y,H-{edge})"
            f"*gt(pow({radius}-X,2)+pow(Y-(H-{edge}),2),{radius_squared}),0,"
            f"if(gt(X,W-{edge})*gt(Y,H-{edge})"
            f"*gt(pow(X-(W-{edge}),2)+pow(Y-(H-{edge}),2),{radius_squared}),0,"
            "alpha(X,Y)))))"
        )
        return [
            (f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expression}'"),
            "format=rgba",
        ]

    def _circle_mask_filters(self, width: int, height: int) -> list[str]:
        center_x = width / 2
        center_y = height / 2
        radius = max(1.0, min(width, height) / 2)
        alpha_expression = (
            "if(lte("
            f"pow(X-{center_x:.3f},2)+pow(Y-{center_y:.3f},2),"
            f"{radius * radius:.3f}),alpha(X,Y),0)"
        )
        return [
            (f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expression}'"),
            "format=rgba",
        ]

    def _diamond_mask_filters(self) -> list[str]:
        alpha_expression = "if(lte(abs(X-W/2)/(W/2)+abs(Y-H/2)/(H/2),1),alpha(X,Y),0)"
        return [
            (f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expression}'"),
            "format=rgba",
        ]

    def _overlay_scale_keyframe_filters(
        self,
        width: int,
        height: int,
        preset: RenderPreset,
        animation: dict,
    ) -> list[str]:
        duration_seconds = self._animation_duration_seconds(animation)
        from_scale = self._animation_scale_value(animation.get("from"), default=1.0)
        to_scale = self._animation_scale_value(animation.get("to"), default=1.0)
        if duration_seconds <= 0 or from_scale == to_scale:
            return []
        keyframe_frames = max(1, int((duration_seconds * preset.fps) + 0.999))
        zoom_expression = self._frame_value_expression(
            from_value=max(from_scale, 1.0),
            to_value=max(to_scale, 1.0),
            keyframe_frames=keyframe_frames,
            easing=str(animation.get("easing") or "linear"),
        )
        return [
            f"zoompan=z={zoom_expression}:d=1:s={width}x{height}:fps={preset.fps}",
            "format=rgba",
        ]

    def _animation_scale_value(self, value: object, default: float) -> float:
        if not isinstance(value, dict):
            return default
        scale = value.get("scale")
        if isinstance(scale, int | float) and scale > 0:
            return float(scale)
        return default

    def _animated_overlay_position(
        self,
        layer: dict,
        target_x: int,
        target_y: int,
        width: int,
        height: int,
        preset: RenderPreset,
    ) -> tuple[str, str]:
        animation = layer.get("animation")
        if not isinstance(animation, dict):
            return str(target_x), str(target_y)
        duration_seconds = self._animation_duration_seconds(animation)
        if duration_seconds <= 0:
            return str(target_x), str(target_y)
        transform = animation.get("rendered_transform")
        easing = str(animation.get("easing") or "linear")
        if transform == "overlay_x_slide":
            return (
                self._time_value_expression(
                    from_value=preset.width,
                    to_value=target_x,
                    duration_seconds=duration_seconds,
                    easing=easing,
                ),
                str(target_y),
            )
        if transform in {"overlay_xy_focus_pop", "overlay_xy_push_in"}:
            from_x = target_x + max(1, int(width * 0.02))
            from_y = target_y + max(1, int(height * 0.02))
            return (
                self._time_value_expression(
                    from_value=from_x,
                    to_value=target_x,
                    duration_seconds=duration_seconds,
                    easing=easing,
                ),
                self._time_value_expression(
                    from_value=from_y,
                    to_value=target_y,
                    duration_seconds=duration_seconds,
                    easing=easing,
                ),
            )
        if transform == "overlay_xy_arc_reveal":
            from_x = target_x + max(1, int(width * 0.22))
            from_y = target_y + max(1, int(height * 0.35))
            return (
                self._time_value_expression(
                    from_value=from_x,
                    to_value=target_x,
                    duration_seconds=duration_seconds,
                    easing=easing,
                ),
                self._arc_time_value_expression(
                    from_value=from_y,
                    to_value=target_y,
                    duration_seconds=duration_seconds,
                    easing=easing,
                    arc_height=max(1, int(height * 0.18)),
                ),
            )
        if transform == "overlay_xy_spotlight_bounce":
            from_x = target_x + max(1, int(width * 0.04))
            from_y = target_y + max(1, int(height * 0.10))
            return (
                self._time_value_expression(
                    from_value=from_x,
                    to_value=target_x,
                    duration_seconds=duration_seconds,
                    easing=easing,
                ),
                self._time_value_expression(
                    from_value=from_y,
                    to_value=target_y,
                    duration_seconds=duration_seconds,
                    easing=easing,
                ),
            )
        return str(target_x), str(target_y)

    def _animation_duration_seconds(self, animation: dict) -> float:
        duration_ms = self._optional_int(animation.get("duration_ms"))
        if duration_ms is None or duration_ms <= 0:
            return 0.0
        return duration_ms / 1000

    def _time_value_expression(
        self,
        from_value: int,
        to_value: int,
        duration_seconds: float,
        easing: str,
    ) -> str:
        if from_value == to_value:
            return str(to_value)
        progress = self._progress_expression(
            variable="t",
            limit=f"{duration_seconds:.3f}",
            easing=easing,
        )
        return (
            "'if(lt(t,"
            f"{duration_seconds:.3f}),"
            f"{from_value}+({to_value - from_value})*({progress}),"
            f"{to_value})'"
        )

    def _arc_time_value_expression(
        self,
        from_value: int,
        to_value: int,
        duration_seconds: float,
        easing: str,
        arc_height: int,
    ) -> str:
        progress = self._progress_expression(
            variable="t",
            limit=f"{duration_seconds:.3f}",
            easing=easing,
        )
        return (
            "'if(lt(t,"
            f"{duration_seconds:.3f}),"
            f"{from_value}+({to_value - from_value})*({progress})"
            f"-{arc_height}*sin(3.14159*({progress})),"
            f"{to_value})'"
        )

    def _frame_value_expression(
        self,
        from_value: float,
        to_value: float,
        keyframe_frames: int,
        easing: str,
    ) -> str:
        if from_value == to_value:
            return f"{to_value:.3f}"
        progress = self._progress_expression(
            variable="on",
            limit=str(keyframe_frames),
            easing=easing,
        )
        return (
            "'if(lt(on,"
            f"{keyframe_frames}),"
            f"{from_value:.3f}+({to_value - from_value:.3f})*({progress}),"
            f"{to_value:.3f})'"
        )

    def _progress_expression(self, variable: str, limit: str, easing: str) -> str:
        progress = f"{variable}/{limit}"
        if easing == "ease_out":
            return f"1-pow(1-({progress}),3)"
        if easing == "ease_in":
            return f"pow({progress},3)"
        if easing == "ease_in_out":
            return f"if(lt({progress},0.5),4*pow({progress},3),1-pow(-2*({progress})+2,3)/2)"
        if easing == "ease_out_back":
            return f"1+2.70158*pow(({progress})-1,3)+1.70158*pow(({progress})-1,2)"
        return progress

    def _visual_overlay_geometry(
        self,
        role: str,
        preset: RenderPreset,
        layout_slot: object = None,
    ) -> tuple[int, int, int, int]:
        if isinstance(layout_slot, dict):
            width = self._optional_int(layout_slot.get("width"))
            height = self._optional_int(layout_slot.get("height"))
            x = self._optional_int(layout_slot.get("x"))
            y = self._optional_int(layout_slot.get("y"))
            if None not in {width, height, x, y}:
                return int(width), int(height), int(x), int(y)
        if role == "video_primary":
            width = int(preset.width * 0.46)
            height = int(preset.height * 0.72)
            return width, height, 48, int(preset.height * 0.16)
        if role == "broll":
            width = int(preset.width * 0.38)
            height = int(preset.height * 0.34)
            return width, height, preset.width - width - 48, 132
        if role == "reaction_loop":
            width = int(preset.width * 0.24)
            height = int(preset.height * 0.24)
            return width, height, preset.width - width - 48, preset.height - height - 58
        width = int(preset.width * 0.30)
        height = int(preset.height * 0.30)
        return width, height, preset.width - width - 48, preset.height - height - 58

    def _image_visual_piece_path(
        self,
        source_path: Path,
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
        transition_policy: dict | None,
        still_motion: str = "push_in",
    ) -> Path:
        output_path = directory / filename
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-loop",
            "1",
            "-i",
            str(source_path),
            "-t",
            f"{max(duration_seconds, 0.001):.3f}",
            "-r",
            str(preset.fps),
            "-vf",
            self._still_visual_filter(
                preset=preset,
                still_motion=still_motion,
                duration_seconds=duration_seconds,
                transition_policy=transition_policy,
            ),
            "-an",
            "-c:v",
            preset.codec,
            "-pix_fmt",
            preset.pixel_format,
            "-b:v",
            preset.video_bitrate,
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._ffmpeg_timeout_seconds(duration_seconds),
            )
        except (subprocess.SubprocessError, OSError):
            return self._generated_visual_piece_path(
                directory=directory,
                filename=filename,
                duration_seconds=duration_seconds,
                preset=preset,
                ffmpeg=ffmpeg,
                color="0x0f172a",
                transition_policy=transition_policy,
            )
        return output_path

    def _video_visual_piece_path(
        self,
        source_path: Path,
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
        transition_policy: dict | None,
        source_start_ms: int = 0,
        source_end_ms: int | None = None,
        virtual_camera: dict | None = None,
    ) -> Path:
        output_path = directory / filename
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-ss",
            f"{max(0, source_start_ms) / 1000:.3f}",
        ]
        if source_end_ms is not None and source_end_ms > source_start_ms:
            command.extend(["-t", f"{(source_end_ms - source_start_ms) / 1000:.3f}"])
        command.extend(
            [
                "-i",
                str(source_path),
                "-t",
                f"{max(duration_seconds, 0.001):.3f}",
                "-r",
                str(preset.fps),
                "-vf",
                self._video_visual_filter(
                    preset,
                    source_end_ms=source_end_ms,
                    source_start_ms=source_start_ms,
                    piece_duration_seconds=duration_seconds,
                    transition_policy=transition_policy,
                    duration_seconds=duration_seconds,
                    virtual_camera=virtual_camera,
                ),
                "-an",
                "-c:v",
                preset.codec,
                "-pix_fmt",
                preset.pixel_format,
                "-b:v",
                preset.video_bitrate,
                str(output_path),
            ]
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._ffmpeg_timeout_seconds(duration_seconds),
            )
        except (subprocess.SubprocessError, OSError):
            return self._generated_visual_piece_path(
                directory=directory,
                filename=filename,
                duration_seconds=duration_seconds,
                preset=preset,
                ffmpeg=ffmpeg,
                color="0x0f172a",
                transition_policy=transition_policy,
            )
        return output_path

    def _generated_visual_piece_path(
        self,
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
        color: str,
        transition_policy: dict | None,
    ) -> Path:
        output_path = directory / filename
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                f"color=c={color}:size={preset.width}x{preset.height}:"
                f"rate={preset.fps}:duration={max(duration_seconds, 0.001):.3f}"
            ),
            "-an",
            "-vf",
            self._append_transition_video_filter(
                f"format={preset.pixel_format}",
                transition_policy,
                duration_seconds,
            ),
            "-c:v",
            preset.codec,
            "-pix_fmt",
            preset.pixel_format,
            "-b:v",
            preset.video_bitrate,
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._ffmpeg_timeout_seconds(duration_seconds),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError(f"ffmpeg visual fallback generation failed: {exc}") from exc
        return output_path

    def _concat_video_pieces(
        self,
        pieces: list[dict],
        directory: Path,
        ffmpeg: str,
        preset: RenderPreset,
    ) -> Path:
        output_path = directory / "visual-plate.mp4"
        # The source clips are not a timing authority. Every piece has already
        # been rendered to its frame allocation, then this re-encode preserves
        # the exact timeline order without container timestamp drift.
        return self._copy_concat_video_pieces(pieces, output_path, directory, ffmpeg, preset)

    def _xfade_video_pieces(
        self,
        pieces: list[dict],
        output_path: Path,
        ffmpeg: str,
        preset: RenderPreset,
    ) -> Path:
        command = [ffmpeg, "-hide_banner", "-y"]
        for piece in pieces:
            command.extend(["-i", str(piece["path"])])

        filter_parts = [
            f"[{index}:v]setpts=PTS-STARTPTS,fps={preset.fps},settb=AVTB,"
            f"format={preset.pixel_format},setsar=1[vx{index}]"
            for index in range(len(pieces))
        ]
        previous_label = "vx0"
        current_duration = float(pieces[0]["duration_seconds"])
        for index, piece in enumerate(pieces[1:], start=1):
            transition_duration = self._piece_xfade_duration_seconds(piece)
            output_label = f"xf{index}"
            if transition_duration > 0:
                current_duration = max(current_duration, transition_duration + 0.001)
                offset = max(0.001, current_duration - transition_duration)
                filter_parts.append(
                    f"[{previous_label}][vx{index}]"
                    "xfade="
                    f"transition={self._xfade_transition_name(piece.get('transition_policy'))}:"
                    f"duration={transition_duration:.3f}:offset={offset:.3f}"
                    f"[{output_label}]"
                )
                current_duration = (
                    current_duration + float(piece["duration_seconds"]) - transition_duration
                )
            else:
                filter_parts.append(
                    f"[{previous_label}][vx{index}]concat=n=2:v=1:a=0[{output_label}]"
                )
                current_duration += float(piece["duration_seconds"])
            previous_label = output_label

        filter_parts.append(f"[{previous_label}]format={preset.pixel_format}[vout]")
        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[vout]",
                "-an",
                "-c:v",
                preset.codec,
                "-pix_fmt",
                preset.pixel_format,
                "-b:v",
                preset.video_bitrate,
                str(output_path),
            ]
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._ffmpeg_timeout_seconds(
                    sum(float(piece.get("duration_seconds") or 0) for piece in pieces),
                    minimum=45,
                ),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            stderr = str(getattr(exc, "stderr", "") or "").strip()
            diagnostic = f"; ffmpeg: {stderr[-1200:]}" if stderr else ""
            raise ValueError(f"ffmpeg xfade visual composition failed: {exc}{diagnostic}") from exc
        return output_path

    def _copy_concat_video_pieces(
        self,
        pieces: list[dict],
        output_path: Path,
        directory: Path,
        ffmpeg: str,
        preset: RenderPreset,
    ) -> Path:
        command = [ffmpeg, "-hide_banner", "-y"]
        for piece in pieces:
            command.extend(["-i", str(piece["path"])])
        labels = [
            f"[{index}:v]setpts=PTS-STARTPTS,fps={preset.fps},format={preset.pixel_format}[v{index}]"
            for index in range(len(pieces))
        ]
        inputs = "".join(f"[v{index}]" for index in range(len(pieces)))
        labels.append(f"{inputs}concat=n={len(pieces)}:v=1:a=0,format={preset.pixel_format}[vout]")
        command.extend(
            [
                "-filter_complex",
                ";".join(labels),
                "-map",
                "[vout]",
                "-an",
                "-c:v",
                preset.codec,
                "-pix_fmt",
                preset.pixel_format,
                "-b:v",
                preset.video_bitrate,
                str(output_path),
            ]
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._ffmpeg_timeout_seconds(
                    sum(float(piece.get("duration_seconds") or 0) for piece in pieces),
                    minimum=30,
                ),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError(f"ffmpeg visual composition failed: {exc}") from exc
        return output_path

    def _piece_xfade_duration_seconds(self, piece: dict) -> float:
        transition_policy = piece.get("transition_policy")
        if not isinstance(transition_policy, dict) or not transition_policy.get("cross_scene"):
            return 0.0
        duration_seconds = self._transition_duration_seconds(transition_policy)
        if duration_seconds <= 0:
            return 0.0
        piece_duration = max(0.001, float(piece.get("duration_seconds") or 0.001))
        return min(duration_seconds, piece_duration / 2)

    @staticmethod
    def _ffmpeg_timeout_seconds(duration_seconds: float, minimum: int = 30) -> int:
        """Allow real-time media composition while keeping failed subprocesses bounded."""
        return max(minimum, min(600, int(max(0.0, duration_seconds) * 2 + 30)))

    @staticmethod
    def _scheduled_video_duration_seconds(timeline: dict, preset: RenderPreset) -> float:
        timeline_duration_seconds = max(0.001, float(timeline.get("duration_ms") or 0) / 1000)
        return max(1.0 / preset.fps, round(timeline_duration_seconds * preset.fps) / preset.fps)

    def _transition_duration_seconds(self, transition_policy: dict | None) -> float:
        if not isinstance(transition_policy, dict):
            return 0.0
        duration_ms = self._optional_int(transition_policy.get("duration_ms"))
        if duration_ms is None or duration_ms <= 0:
            return 0.0
        return duration_ms / 1000

    def _xfade_transition_name(self, transition_policy: dict | None) -> str:
        return "fade"

    def _visual_normalization_filter(
        self,
        preset: RenderPreset,
        transition_policy: dict | None = None,
        duration_seconds: float | None = None,
    ) -> str:
        base_filter = (
            f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
            f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih):color=0x020617,"
            f"fps={preset.fps},format={preset.pixel_format}"
        )
        return self._append_transition_video_filter(
            base_filter,
            transition_policy,
            duration_seconds,
        )

    def _still_visual_filter(
        self,
        *,
        preset: RenderPreset,
        still_motion: str,
        duration_seconds: float,
        transition_policy: dict | None,
    ) -> str:
        """Create restrained motion for credited stills without inventing visual content."""
        frame_count = max(1, round(max(duration_seconds, 0.001) * preset.fps))
        progress = f"on/{frame_count}"
        if still_motion == "pan_left":
            zoom = "min(zoom+0.0006,1.07)"
            x = f"(iw-iw/zoom)*{progress}"
        elif still_motion == "pan_right":
            zoom = "min(zoom+0.0006,1.07)"
            x = f"(iw-iw/zoom)*(1-{progress})"
        elif still_motion == "pull_back":
            zoom = "if(eq(on,0),1.08,max(zoom-0.0006,1.0))"
            x = "(iw-iw/zoom)/2"
        else:
            zoom = "min(zoom+0.0007,1.08)"
            x = "(iw-iw/zoom)/2"
        base_filter = (
            f"scale={preset.width * 2}:{preset.height * 2}:force_original_aspect_ratio=increase,"
            f"crop={preset.width * 2}:{preset.height * 2},"
            f"zoompan=z='{zoom}':x='{x}':y='(ih-ih/zoom)/2':d=1:"
            f"s={preset.width}x{preset.height}:fps={preset.fps},format={preset.pixel_format}"
        )
        return self._append_transition_video_filter(
            base_filter,
            transition_policy,
            duration_seconds,
        )

    def _video_visual_filter(
        self,
        preset: RenderPreset,
        *,
        source_start_ms: int,
        source_end_ms: int | None,
        piece_duration_seconds: float,
        transition_policy: dict | None,
        duration_seconds: float,
        virtual_camera: dict | None = None,
    ) -> str:
        base_filter = (
            self._seated_panel_virtual_camera_filter(
                preset,
                virtual_camera,
                duration_seconds=piece_duration_seconds,
            )
            if virtual_camera is not None
            else self._visual_normalization_filter(preset)
        )
        # Native talking-head clips may end a few frames short. Clone the last
        # frame and trim to the timeline allocation so a short source can never
        # pull later audio out of sync.
        base_filter += (
            f",tpad=stop_mode=clone:stop_duration={max(piece_duration_seconds, 0.001):.3f}"
            f",trim=duration={max(piece_duration_seconds, 0.001):.3f},setpts=PTS-STARTPTS"
        )
        return self._append_transition_video_filter(
            base_filter,
            transition_policy,
            duration_seconds,
        )

    def _seated_panel_virtual_camera(
        self,
        *,
        segment: dict,
        asset_by_id: dict[str, Asset],
    ) -> dict | None:
        """Translate the editorial view into a deterministic crop of B1's master plate."""
        direction = segment.get("direction")
        if not isinstance(direction, dict) or (
            direction.get("speaker_mouth_mode") != "audio_driven_seated_panel"
        ):
            return None
        view = str(direction.get("view") or segment.get("camera_view") or "establishing_wide")
        action = str(direction.get("action") or segment.get("camera_action") or "cut")
        motion_actions = {"fly_in", "slow_push", "slow_pull", "pan_left", "pan_right"}
        if view == "establishing_wide" and action not in motion_actions:
            return None
        primary = asset_by_id.get(str(segment.get("video_asset_id") or ""))
        if (
            primary is not None
            and primary.generation_metadata.get("provider_studio_panel_camera_composition")
            == "native_scene_camera"
        ):
            # B1 already rendered the configured speaker/two-shot framing.
            # Cropping it again would discard the native camera coverage.
            return None
        shot_plan = (
            primary.generation_metadata.get("shot_plan")
            if primary is not None and isinstance(primary.generation_metadata, dict)
            else {}
        )
        if not isinstance(shot_plan, dict):
            shot_plan = {}
        speaker_id = str(segment.get("speaker_id") or "")
        paired_ids = shot_plan.get("paired_participant_ids")
        participant_ids = [speaker_id] if speaker_id else []
        if isinstance(paired_ids, list):
            participant_ids.extend(
                participant_id
                for participant_id in paired_ids
                if isinstance(participant_id, str) and participant_id
            )
        scene = asset_by_id.get(str(segment.get("studio_panel_scene_asset_id") or ""))
        positions = self._seated_panel_focus_positions(
            scene=scene,
            participant_ids=participant_ids,
            seating_plan=shot_plan.get("seating_plan"),
        )
        # Speaker shots are composed around the active speaker.  A paired
        # participant is useful framing context, but averaging both faces makes
        # the speaker land at the edge of the crop and can make the silent face
        # more prominent.  Preserve the pair in metadata while anchoring the
        # camera on the first (speaker) position.
        focus_x, focus_y = positions[0]
        scale_by_view = {
            "establishing_wide": 1.0,
            "panel_two_shot": 1.24,
            "speaker_medium": 1.48,
            "speaker_close_up": 1.92,
            "reaction": 1.48,
        }
        scale = scale_by_view.get(view, 1.0)
        if action == "slow_push":
            scale *= 1.04
        if scale <= 1.0 and action not in motion_actions:
            return None
        normalized_pair_ids = [
            participant_id
            for participant_id in participant_ids[1:]
            if participant_id and participant_id != speaker_id
        ]
        return {
            "schema_version": "dialecticore.virtual_camera.v3",
            "view": view,
            "scale": round(scale, 4),
            "focus_x": round(min(0.96, max(0.04, focus_x)), 5),
            "focus_y": round(min(0.92, max(0.08, focus_y)), 5),
            "focus_source": "active_speaker_face_region_or_seating_plan",
            "speaker_participant_id": speaker_id or None,
            "context_participant_ids": normalized_pair_ids,
            "motion": action if action in motion_actions else None,
            "framing_policy": {
                "speaker_target_frame_x": 0.5,
                "speaker_allowed_frame_x": [0.45, 0.55],
                "speaker_must_be_primary": True,
                "retain_head_shoulders": True,
                "retain_desk_context": True,
            },
        }

    def _seated_panel_focus_positions(
        self,
        *,
        scene: Asset | None,
        participant_ids: list[str],
        seating_plan: object,
    ) -> list[tuple[float, float]]:
        studio_panel = (
            scene.generation_metadata.get("studio_panel")
            if scene is not None and isinstance(scene.generation_metadata, dict)
            else {}
        )
        if not isinstance(studio_panel, dict):
            studio_panel = {}
        face_regions = studio_panel.get("face_regions") or studio_panel.get("seat_map")
        positions = [
            position
            for participant_id in participant_ids
            if (position := self._normalized_face_position(face_regions, participant_id))
            is not None
        ]
        if positions:
            return positions
        normalized_seating = (
            {
                str(participant_id): int(seat)
                for participant_id, seat in seating_plan.items()
                if isinstance(participant_id, str) and isinstance(seat, int) and seat > 0
            }
            if isinstance(seating_plan, dict)
            else {}
        )
        ordered_ids = [
            participant_id
            for participant_id, _seat in sorted(
                normalized_seating.items(), key=lambda item: item[1]
            )
        ]
        fallback_positions = []
        for participant_id in participant_ids:
            try:
                index = ordered_ids.index(participant_id)
            except ValueError:
                continue
            fallback_positions.append(((index + 0.5) / max(1, len(ordered_ids)), 0.46))
        return fallback_positions or [(0.5, 0.46)]

    def _normalized_face_position(
        self,
        face_regions: object,
        participant_id: str,
    ) -> tuple[float, float] | None:
        entries = [face_regions] if isinstance(face_regions, dict) else face_regions
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_participant_id = str(
                entry.get("participant_id")
                or entry.get("speaker_participant_id")
                or entry.get("id")
                or ""
            )
            if entry_participant_id != participant_id:
                continue
            region = next(
                (
                    entry.get(key)
                    for key in ("face_region", "region", "bbox")
                    if isinstance(entry.get(key), dict)
                ),
                entry,
            )
            if not isinstance(region, dict):
                continue
            center_x = self._optional_float(region.get("center_x"))
            center_y = self._optional_float(region.get("center_y"))
            if center_x is not None and center_y is not None:
                if 0 <= center_x <= 1 and 0 <= center_y <= 1:
                    return center_x, center_y
                continue
            x = self._optional_float(region.get("x") if "x" in region else region.get("left"))
            y = self._optional_float(region.get("y") if "y" in region else region.get("top"))
            width = self._optional_float(
                region.get("width") if "width" in region else region.get("w")
            )
            height = self._optional_float(
                region.get("height") if "height" in region else region.get("h")
            )
            if None not in {x, y, width, height} and all(
                0 <= value <= 1 for value in (x, y, width, height)
            ):
                return x + width / 2, y + height / 2
        return None

    def _seated_panel_virtual_camera_filter(
        self,
        preset: RenderPreset,
        virtual_camera: dict,
        duration_seconds: float | None = None,
    ) -> str:
        scale = self._optional_float(virtual_camera.get("scale")) or 1.0
        focus_x = self._optional_float(virtual_camera.get("focus_x")) or 0.5
        focus_y = self._optional_float(virtual_camera.get("focus_y")) or 0.46
        scale = min(2.2, max(1.01, scale))
        focus_x = min(0.96, max(0.04, focus_x))
        focus_y = min(0.92, max(0.08, focus_y))
        motion = str(virtual_camera.get("motion") or "")
        if motion in {"fly_in", "slow_push", "slow_pull", "pan_left", "pan_right"}:
            frame_count = max(
                1,
                round(max(0.001, float(duration_seconds or 0.001)) * preset.fps),
            )
            progress = f"on/{max(1, frame_count - 1)}"
            if motion == "fly_in":
                start_scale, end_scale = 1.0, max(1.08, scale)
            elif motion == "slow_pull":
                start_scale, end_scale = min(2.2, scale * 1.04), scale
            elif motion == "slow_push":
                start_scale, end_scale = max(1.0, scale / 1.04), scale
            else:
                start_scale = end_scale = scale
            pan_start = 0.04 if motion == "pan_left" else -0.04 if motion == "pan_right" else 0
            pan_end = -pan_start
            zoom = f"{start_scale:.4f}+({end_scale - start_scale:.4f})*{progress}"
            focus = f"{focus_x:.5f}+({pan_start:.4f}+({pan_end - pan_start:.4f})*{progress})"
            return (
                f"scale={preset.width * 2}:{preset.height * 2}:"
                "force_original_aspect_ratio=increase,"
                f"crop={preset.width * 2}:{preset.height * 2},"
                f"zoompan=z='{zoom}':"
                f"x='max(0,min(iw-iw/zoom,iw*({focus})-iw/zoom/2))':"
                f"y='max(0,min(ih-ih/zoom,ih*{focus_y:.5f}-ih/zoom/2))':"
                f"d=1:s={preset.width}x{preset.height}:fps={preset.fps},"
                f"format={preset.pixel_format}"
            )
        return (
            f"crop=w='trunc(iw/{scale:.4f}/2)*2':h='trunc(ih/{scale:.4f}/2)*2':"
            f"x='max(0,min(iw-ow,iw*{focus_x:.5f}-ow/2))':"
            f"y='max(0,min(ih-oh,ih*{focus_y:.5f}-oh/2))',"
            f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=increase,"
            f"crop={preset.width}:{preset.height},fps={preset.fps},format={preset.pixel_format}"
        )

    def _append_transition_video_filter(
        self,
        filter_text: str,
        transition_policy: dict | None,
        duration_seconds: float | None,
    ) -> str:
        if not isinstance(transition_policy, dict):
            return filter_text
        filter_with_transitions = filter_text
        maximum_fade_seconds = max(0.001, float(duration_seconds or 0.001) / 2)
        duration_ms = self._optional_int(transition_policy.get("duration_ms")) or 0
        if duration_ms > 0 and transition_policy.get("whole_scene_fade") is not False:
            transition_seconds = min(duration_ms / 1000, maximum_fade_seconds)
            filter_with_transitions += f",fade=t=in:st=0:d={transition_seconds:.3f}"
        terminal_fade_out_ms = (
            self._optional_int(transition_policy.get("terminal_fade_out_ms")) or 0
        )
        if terminal_fade_out_ms > 0:
            fade_out_seconds = min(terminal_fade_out_ms / 1000, maximum_fade_seconds)
            fade_out_start_seconds = max(0.0, float(duration_seconds or 0.001) - fade_out_seconds)
            filter_with_transitions += (
                f",fade=t=out:st={fade_out_start_seconds:.3f}:d={fade_out_seconds:.3f}"
            )
        return filter_with_transitions

    def _dialogue_audio_mix_path(
        self,
        episode: Episode,
        timeline: dict,
        preset: RenderPreset,
        duration_seconds: float,
        directory: Path,
        ffmpeg: str,
    ) -> Path:
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        segments = [
            segment
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict)
            and float(segment.get("start_ms") or 0) < duration_seconds * 1000
        ]
        pieces: list[Path] = []
        cursor_seconds = 0.0
        for index, segment in enumerate(segments, start=1):
            start_seconds = max(0.0, float(segment.get("start_ms") or 0) / 1000)
            end_seconds = min(
                duration_seconds,
                float(segment.get("end_ms") or 0) / 1000,
            )
            if end_seconds <= start_seconds:
                continue
            if start_seconds > cursor_seconds:
                pieces.append(
                    self._silent_audio_piece_path(
                        directory=directory,
                        filename=f"audio-gap-{index:04d}.wav",
                        duration_seconds=start_seconds - cursor_seconds,
                        preset=preset,
                        ffmpeg=ffmpeg,
                    )
                )
            piece_duration_seconds = end_seconds - start_seconds
            audio_asset = asset_by_id.get(str(segment.get("audio_asset_id") or ""))
            audio_path = self._optional_path_for_asset(audio_asset)
            if audio_path is not None:
                pieces.append(
                    self._normalized_audio_piece_path(
                        source_path=audio_path,
                        directory=directory,
                        filename=f"audio-segment-{index:04d}.wav",
                        duration_seconds=piece_duration_seconds,
                        source_offset_seconds=max(
                            0.0, float(segment.get("audio_source_offset_ms") or 0) / 1000
                        ),
                        preset=preset,
                        ffmpeg=ffmpeg,
                    )
                )
            else:
                pieces.append(
                    self._silent_audio_piece_path(
                        directory=directory,
                        filename=f"audio-silence-{index:04d}.wav",
                        duration_seconds=piece_duration_seconds,
                        preset=preset,
                        ffmpeg=ffmpeg,
                    )
                )
            cursor_seconds = end_seconds

        if cursor_seconds < duration_seconds:
            pieces.append(
                self._silent_audio_piece_path(
                    directory=directory,
                    filename="audio-tail.wav",
                    duration_seconds=duration_seconds - cursor_seconds,
                    preset=preset,
                    ffmpeg=ffmpeg,
                )
            )
        if not pieces:
            pieces.append(
                self._silent_audio_piece_path(
                    directory=directory,
                    filename="audio-empty.wav",
                    duration_seconds=duration_seconds,
                    preset=preset,
                    ffmpeg=ffmpeg,
                )
            )
        return self._concat_audio_pieces(
            pieces=pieces,
            directory=directory,
            ffmpeg=ffmpeg,
        )

    def _normalized_audio_piece_path(
        self,
        source_path: Path,
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
        source_offset_seconds: float = 0.0,
    ) -> Path:
        output_path = directory / filename
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-ss",
            f"{max(source_offset_seconds, 0.0):.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{max(duration_seconds, 0.001):.3f}",
            "-vn",
            "-af",
            (
                f"aresample={preset.audio_sample_rate},"
                f"aformat=channel_layouts={preset.audio_layout},"
                f"apad,atrim=0:{max(duration_seconds, 0.001):.3f}"
            ),
            "-ac",
            str(self._audio_channel_count(preset.audio_layout)),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.SubprocessError, OSError):
            return self._silent_audio_piece_path(
                directory=directory,
                filename=filename,
                duration_seconds=duration_seconds,
                preset=preset,
                ffmpeg=ffmpeg,
            )
        return output_path

    def _silent_audio_piece_path(
        self,
        directory: Path,
        filename: str,
        duration_seconds: float,
        preset: RenderPreset,
        ffmpeg: str,
    ) -> Path:
        output_path = directory / filename
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                "anullsrc="
                f"channel_layout={preset.audio_layout}:"
                f"sample_rate={preset.audio_sample_rate}"
            ),
            "-t",
            f"{max(duration_seconds, 0.001):.3f}",
            "-ac",
            str(self._audio_channel_count(preset.audio_layout)),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError(f"ffmpeg silence generation failed: {exc}") from exc
        return output_path

    def _concat_audio_pieces(
        self,
        pieces: list[Path],
        directory: Path,
        ffmpeg: str,
    ) -> Path:
        output_path = directory / "dialogue-mix.wav"
        if len(pieces) == 1:
            shutil.copyfile(pieces[0], output_path)
            return output_path
        concat_path = directory / "dialogue-concat.txt"
        concat_path.write_text(
            "\n".join(f"file '{piece.as_posix()}'" for piece in pieces),
            encoding="utf-8",
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError(f"ffmpeg audio composition failed: {exc}") from exc
        return output_path

    def _scene_video_filter(
        self,
        episode: Episode,
        timeline: dict,
        preset: RenderPreset,
        duration_seconds: float,
    ) -> str:
        filters: list[str] = []
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        subtitle_cache: dict[str, list[dict]] = {}
        segments = [
            segment
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict)
            and float(segment.get("start_ms") or 0) < duration_seconds * 1000
        ]
        for _index, segment in enumerate(segments):
            start = max(0.0, float(segment.get("start_ms") or 0) / 1000)
            end = min(duration_seconds, float(segment.get("end_ms") or 0) / 1000)
            if end <= start:
                continue
            enable = self._between_enable(start, end)
            speaker_id = str(segment.get("speaker_id") or "").strip()
            if speaker_id:
                speaker = self._ffmpeg_text(speaker_id)
                filters.extend(
                    [
                        (
                            # drawbox's `h` is the box height, not the input
                            # frame height.  Use `ih` so the backing plate and
                            # drawtext stay in the same bottom-left lower third.
                            "drawbox=x=48:y=ih-126:w=540:h=76:"
                            f"color=0x020617@0.78:t=fill:enable='{enable}'"
                        ),
                        (
                            f"drawtext=text='{speaker}':x=76:y=h-100:"
                            "fontsize=34:fontcolor=0xffffff:"
                            f"enable='{enable}'"
                        ),
                    ]
                )
            caption_cues = (
                self._subtitle_cues_for_segment(segment, asset_by_id, subtitle_cache)
                if timeline.get("media", {}).get("subtitle_mode") == "burned_in"
                else []
            )
            for cue in caption_cues:
                cue_start = max(start, float(cue["start_ms"]) / 1000)
                cue_end = min(end, float(cue["end_ms"]) / 1000)
                if cue_end <= cue_start:
                    continue
                cue_enable = self._between_enable(cue_start, cue_end)
                caption_lines = self._caption_lines(str(cue.get("text") or ""))
                if not caption_lines:
                    continue
                caption_x = int(preset.width * 0.14)
                caption_width = int(preset.width * 0.72)
                caption_height = 96 if len(caption_lines) == 1 else 132
                caption_y = int(preset.height * 0.68)
                caption_text_x = caption_x + 28
                caption_text_y = caption_y + 24
                filters.extend(
                    [
                        (
                            f"drawbox=x={caption_x}:y={caption_y}:"
                            f"w={caption_width}:h={caption_height}:"
                            "color=0x020617@0.82:t=fill:"
                            f"enable='{cue_enable}'"
                        ),
                        (
                            f"drawbox=x={caption_x}:y={caption_y}:"
                            f"w={caption_width}:h={caption_height}:"
                            "color=0xf8fafc@0.85:t=2:"
                            f"enable='{cue_enable}'"
                        ),
                    ]
                )
                for line_index, line in enumerate(caption_lines):
                    filters.append(
                        f"drawtext=text='{self._ffmpeg_text(line)}':"
                        f"x={caption_text_x}:"
                        f"y={caption_text_y + line_index * 38}:"
                        "fontsize=30:fontcolor=0xf8fafc:"
                        f"enable='{cue_enable}'"
                    )
        filters.append("format=yuv420p")
        return ",".join(filters)

    @staticmethod
    def _thumbnail_seek_seconds(render_asset: Asset) -> float:
        manifest = render_asset.generation_metadata.get("render_manifest")
        composition = manifest.get("composition") if isinstance(manifest, dict) else None
        layers = composition.get("segment_layers") if isinstance(composition, dict) else None
        if isinstance(layers, list):
            for layer in layers:
                if not isinstance(layer, dict):
                    continue
                layout = layer.get("layout_policy")
                if not isinstance(layout, dict) or layout.get("name") != (
                    "seated_panel_rear_screen_cutaway"
                ):
                    continue
                start_ms = layer.get("start_ms")
                duration_ms = layer.get("duration_ms")
                if isinstance(start_ms, (int, float)) and isinstance(duration_ms, (int, float)):
                    return round((start_ms + duration_ms / 2) / 1000, 3)

        duration_ms = render_asset.duration_ms
        if not duration_ms:
            probe = render_asset.generation_metadata.get("media_probe")
            duration_ms = probe.get("duration_ms") if isinstance(probe, dict) else None
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            duration_seconds = duration_ms / 1000
            if duration_seconds <= 2:
                return round(duration_seconds / 2, 3)
            return round(min(max(duration_seconds * 0.25, 1.0), duration_seconds - 1), 3)
        return 1.0

    def _thumbnail_bytes(
        self,
        render_path: Path,
        *,
        seek_seconds: float,
    ) -> bytes:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise ValueError("ffmpeg is not available for thumbnail generation")
        with tempfile.TemporaryDirectory(prefix="dialecticore-thumbnail-") as directory:
            output_path = Path(directory) / "thumbnail.jpg"
            command = [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-ss",
                f"{seek_seconds:.3f}",
                "-i",
                str(render_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output_path),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise ValueError(f"ffmpeg thumbnail generation failed: {exc}") from exc
            return output_path.read_bytes()

    def _thumbnail_average_luma(self, thumbnail_path: Path) -> int | None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return None
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(thumbnail_path),
            "-vf",
            "scale=1:1:flags=area,format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return completed.stdout[0] if completed.stdout else None

    def _youtube_package_manifest(
        self,
        episode: Episode,
        render_asset: Asset,
        thumbnail_asset: Asset | None,
        package_id: str,
    ) -> dict:
        render_manifest = render_asset.generation_metadata.get("render_manifest", {})
        timeline = self._timeline_from_render_asset(episode, render_asset)
        evidence_lineage = (
            render_manifest.get("evidence_lineage", {}) if isinstance(render_manifest, dict) else {}
        )
        chapters = self._timeline_chapters(timeline)
        tags = [
            "DialectiCore",
            episode.definition.topic.central_question,
            *episode.definition.topic.required_dimensions,
        ]
        return {
            "id": package_id,
            "schema_version": "youtube_package.v1",
            "episode_id": str(episode.id),
            "title": episode.title,
            "description": self._youtube_description(episode, chapters),
            "tags": [tag for tag in tags if tag],
            "language": render_asset.language or episode.source_language,
            "render_asset_id": str(render_asset.id),
            "render_uri": render_asset.storage_uri,
            "render_checksum": render_asset.checksum,
            "render_type": render_asset.generation_metadata.get("render_type"),
            "preset_id": render_asset.generation_metadata.get("preset_id"),
            "thumbnail_asset_id": str(thumbnail_asset.id) if thumbnail_asset else None,
            "thumbnail_uri": thumbnail_asset.storage_uri if thumbnail_asset else None,
            "thumbnail_checksum": thumbnail_asset.checksum if thumbnail_asset else None,
            "chapters": chapters,
            "subtitles": self._subtitle_entries(episode, render_manifest),
            "evidence_lineage": evidence_lineage,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def _youtube_package_bytes(
        self,
        episode: Episode,
        manifest: dict,
        render_asset: Asset,
        thumbnail_asset: Asset | None,
    ) -> tuple[bytes, list[str]]:
        buffer = BytesIO()
        included_files = ["youtube-package.json"]
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "youtube-package.json",
                json.dumps(manifest, indent=2, sort_keys=True),
            )
            render_path = self._path_for_asset(render_asset)
            archive.write(render_path, f"video/render.{self._extension_for_asset(render_asset)}")
            included_files.append(f"video/render.{self._extension_for_asset(render_asset)}")
            if thumbnail_asset is not None:
                thumbnail_path = self._path_for_asset(thumbnail_asset)
                archive.write(thumbnail_path, "thumbnail/thumbnail.jpg")
                included_files.append("thumbnail/thumbnail.jpg")
            for subtitle_asset in self._subtitle_assets_for_package(episode, render_asset):
                subtitle_name = self._subtitle_filename(subtitle_asset)
                subtitle_path = self.object_store.path_for_uri(subtitle_asset.storage_uri or "")
                if subtitle_path is not None and subtitle_path.exists():
                    archive.write(subtitle_path, subtitle_name)
                    included_files.append(subtitle_name)
                    continue
                subtitle_text = subtitle_asset.generation_metadata.get("subtitle_text")
                if isinstance(subtitle_text, str) and subtitle_text:
                    archive.writestr(subtitle_name, subtitle_text)
                    included_files.append(subtitle_name)
        return buffer.getvalue(), included_files

    def _production_manifest(
        self,
        episode: Episode,
        manifest_id: str,
        package_asset: Asset,
        render_asset: Asset,
    ) -> dict:
        render_manifest = render_asset.generation_metadata.get("render_manifest", {})
        if not isinstance(render_manifest, dict):
            render_manifest = {}
        package_manifest = package_asset.generation_metadata.get("youtube_package_manifest", {})
        if not isinstance(package_manifest, dict):
            package_manifest = {}
        timeline = self._timeline_from_render_asset(episode, render_asset)
        talkshow_visuals = self._production_talkshow_visuals(episode, timeline)
        return {
            "id": manifest_id,
            "schema_version": "production_manifest.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "episode": {
                "id": str(episode.id),
                "title": episode.title,
                "slug": episode.slug,
                "subject": episode.subject,
                "central_question": episode.central_question,
                "status": episode.status.value,
                "source_language": episode.source_language,
                "target_duration_seconds": episode.target_duration_seconds,
                "canonical_transcript_version_id": (
                    str(episode.canonical_transcript_version_id)
                    if episode.canonical_transcript_version_id
                    else None
                ),
                "created_at": episode.created_at.isoformat(),
                "updated_at": episode.updated_at.isoformat(),
            },
            "configuration": {
                "format": episode.definition.format.model_dump(mode="json"),
                "languages": episode.definition.languages.model_dump(mode="json"),
                "research": episode.definition.research.model_dump(mode="json"),
                "media": episode.definition.media.model_dump(mode="json"),
                "workflow": episode.definition.workflow.model_dump(mode="json"),
                "quality": episode.definition.quality.model_dump(mode="json"),
            },
            "participants": [
                {
                    "id": participant.id,
                    "display_name": participant.display_name,
                    "participant_type": participant.participant_type.value,
                    "model_endpoint_id": participant.model_endpoint_id,
                    "model_id": participant.model_id,
                    "system_prompt_template": participant.system_prompt_template,
                    "voice_profile_id": participant.voice_profile_id,
                    "visual_profile_id": participant.visual_profile_id,
                    "enabled": participant.enabled,
                }
                for participant in episode.participants
            ],
            "model_endpoints": [
                {
                    "id": endpoint.id,
                    "provider_type": endpoint.provider_type.value,
                    "health_status": endpoint.health_status,
                    "credential_reference_configured": endpoint.credential_reference is not None,
                    "capabilities": self._safe_manifest_payload(endpoint.capabilities),
                }
                for endpoint in episode.model_endpoints
            ],
            "workflow": {
                "control": episode.workflow_control,
                "approval_count": len(episode.approvals),
                "audit_event_count": len(episode.audit_events),
            },
            "transcripts": [
                {
                    "id": str(transcript.id),
                    "type": transcript.type.value,
                    "language": transcript.language,
                    "status": transcript.status,
                    "parent_version_id": (
                        str(transcript.parent_version_id) if transcript.parent_version_id else None
                    ),
                    "turn_count": len(transcript.turns),
                    "claim_count": sum(len(turn.claims) for turn in transcript.turns),
                    "source_link_count": sum(
                        len(turn.source_discussion_turn_ids) for turn in transcript.turns
                    ),
                    "semantic_fidelity_score": transcript.semantic_fidelity_score,
                    "localization_metadata": self._safe_manifest_payload(
                        transcript.localization_metadata
                    ),
                    "turn_lineage": [
                        {
                            "transcript_turn_id": str(turn.id),
                            "speaker_participant_id": turn.speaker_participant_id,
                            "source_discussion_turn_ids": [
                                str(turn_id) for turn_id in turn.source_discussion_turn_ids
                            ],
                            "claim_count": len(turn.claims),
                            "status": turn.status,
                        }
                        for turn in transcript.turns
                    ],
                }
                for transcript in episode.transcripts
            ],
            "timeline": {
                "id": timeline.get("id"),
                "asset_id": render_asset.source_entity_id,
                "language": timeline.get("language"),
                "duration_ms": timeline.get("duration_ms"),
                "segment_count": len(timeline.get("segments", [])),
                "chapter_count": len(self._timeline_chapters(timeline)),
                "chapters": self._timeline_chapters(timeline),
            },
            "timeline_segments": self._production_timeline_segments(timeline),
            "talkshow_visuals": talkshow_visuals,
            "render": {
                "asset_id": str(render_asset.id),
                "storage_uri": render_asset.storage_uri,
                "checksum": render_asset.checksum,
                "render_type": render_asset.generation_metadata.get("render_type"),
                "preset_id": render_asset.generation_metadata.get("preset_id"),
                "manifest_uri": render_asset.generation_metadata.get("render_manifest_uri"),
                "manifest_checksum": render_asset.generation_metadata.get(
                    "render_manifest_checksum"
                ),
                "manifest": render_manifest,
            },
            "delivery_package": {
                "asset_id": str(package_asset.id),
                "storage_uri": package_asset.storage_uri,
                "checksum": package_asset.checksum,
                "package_id": package_asset.generation_metadata.get("package_id"),
                "included_files": package_asset.generation_metadata.get("included_files", []),
                "manifest": package_manifest,
            },
            "assets": [
                self._production_asset_entry(asset, episode)
                for asset in episode.assets
                if asset.asset_type != AssetType.production_manifest
            ],
            "production_manifest_history": [
                {
                    "asset_id": str(asset.id),
                    "status": asset.status,
                    "package_asset_id": asset.source_entity_id,
                    "storage_uri": asset.storage_uri,
                    "checksum": asset.checksum,
                    "created_at": asset.created_at.isoformat(),
                }
                for asset in episode.assets
                if asset.asset_type == AssetType.production_manifest
            ],
            "quality_results": [
                {
                    "id": str(result.id),
                    "target_type": result.target_type,
                    "target_id": result.target_id,
                    "check_type": result.check_type,
                    "status": result.status,
                    "severity": result.severity.value,
                    "score": result.score,
                    "created_at": result.created_at.isoformat(),
                    "details": self._safe_manifest_payload(result.details),
                }
                for result in episode.quality_results
            ],
            "approvals": [
                {
                    "id": str(approval.id),
                    "stage": approval.stage,
                    "decision": approval.decision,
                    "user_id": approval.user_id,
                    "created_at": approval.created_at.isoformat(),
                }
                for approval in episode.approvals
            ],
            "publish_jobs": [
                {
                    "id": str(job.id),
                    "publisher_target_id": job.publisher_target_id,
                    "platform": job.platform,
                    "package_asset_id": str(job.package_asset_id),
                    "status": job.status,
                    "dry_run": job.dry_run,
                    "remote_job_id": job.remote_job_id,
                    "publish_url": job.publish_url,
                    "requested_at": job.requested_at.isoformat(),
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "result_metadata": self._safe_manifest_payload(job.result_metadata),
                    "delivery_payload": self._safe_manifest_payload(job.delivery_payload),
                }
                for job in episode.publish_jobs
                if str(job.package_asset_id) == str(package_asset.id)
            ],
            "evidence_lineage": (
                package_manifest.get("evidence_lineage")
                or render_manifest.get("evidence_lineage")
                or {}
            ),
        }

    def _production_timeline_segments(self, timeline: dict) -> list[dict]:
        segments = []
        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            segments.append(
                {
                    "id": segment.get("id"),
                    "start_ms": segment.get("start_ms"),
                    "end_ms": segment.get("end_ms"),
                    "speaker_id": segment.get("speaker_id"),
                    "source_turn_id": segment.get("source_turn_id"),
                    "source_discussion_turn_ids": segment.get("source_discussion_turn_ids", []),
                    "audio_asset_id": segment.get("audio_asset_id"),
                    "video_asset_id": segment.get("video_asset_id"),
                    "secondary_visual_asset_id": segment.get("secondary_visual_asset_id"),
                    "reaction_visual_asset_id": segment.get("reaction_visual_asset_id"),
                    "studio_scene_asset_id": segment.get("studio_scene_asset_id"),
                    "fallback_video_asset_id": segment.get("fallback_video_asset_id"),
                    "subtitle_asset_id": segment.get("subtitle_asset_id"),
                    "citation_overlay_asset_ids": segment.get("citation_overlay_asset_ids", []),
                    "visual_layers": segment.get("visual_layers", []),
                    "evidence_refs": segment.get("evidence_refs", []),
                    "camera_transition": segment.get("camera_transition"),
                    "visual_role": segment.get("visual_role"),
                }
            )
        return segments

    def _production_talkshow_visuals(self, episode: Episode, timeline: dict) -> dict:
        assets_by_id = {
            str(asset.id): asset for asset in episode.assets if asset.status != "replaced"
        }
        expected_reaction_segment_ids = set()
        linked_reaction_segment_ids = set()
        expected_studio_segment_ids = set()
        linked_studio_segment_ids = set()
        reaction_asset_ids = set()
        studio_asset_ids = set()

        for segment in timeline.get("segments", []):
            if not isinstance(segment, dict):
                continue
            segment_id = self._production_segment_id(segment)
            primary_asset_id = segment.get("video_asset_id")
            primary_asset = (
                assets_by_id.get(primary_asset_id) if isinstance(primary_asset_id, str) else None
            )
            shot_plan = (
                primary_asset.generation_metadata.get("shot_plan")
                if primary_asset is not None
                else None
            )
            if not isinstance(shot_plan, dict):
                continue
            expected_reaction_id = self._non_empty_string(
                shot_plan.get("reusable_reaction_asset_id")
            )
            if expected_reaction_id is not None:
                expected_reaction_segment_ids.add(segment_id)
                reaction_asset_ids.add(expected_reaction_id)
                expected_reaction = assets_by_id.get(expected_reaction_id)
                if segment.get(
                    "reaction_visual_asset_id"
                ) == expected_reaction_id and self._asset_completed_render_ready(
                    expected_reaction,
                    AssetType.reaction_loop,
                ):
                    linked_reaction_segment_ids.add(segment_id)
            expected_studio_id = self._non_empty_string(shot_plan.get("studio_scene_asset_id"))
            if expected_studio_id is not None:
                expected_studio_segment_ids.add(segment_id)
                studio_asset_ids.add(expected_studio_id)
                expected_studio = assets_by_id.get(expected_studio_id)
                if segment.get(
                    "studio_scene_asset_id"
                ) == expected_studio_id and self._asset_completed_render_ready(
                    expected_studio,
                    AssetType.studio_scene,
                ):
                    linked_studio_segment_ids.add(segment_id)

        missing_reaction_segment_ids = sorted(
            expected_reaction_segment_ids - linked_reaction_segment_ids
        )
        missing_studio_segment_ids = sorted(expected_studio_segment_ids - linked_studio_segment_ids)
        return {
            "schema_version": "talkshow_visual_handoff.v1",
            "reaction_loop": {
                "expected_segment_count": len(expected_reaction_segment_ids),
                "linked_segment_count": len(linked_reaction_segment_ids),
                "missing_segment_ids": missing_reaction_segment_ids,
                "asset_ids": sorted(reaction_asset_ids),
                "ready": not missing_reaction_segment_ids,
            },
            "studio_scene": {
                "expected_segment_count": len(expected_studio_segment_ids),
                "linked_segment_count": len(linked_studio_segment_ids),
                "missing_segment_ids": missing_studio_segment_ids,
                "asset_ids": sorted(studio_asset_ids),
                "ready": not missing_studio_segment_ids,
            },
            "ready": not missing_reaction_segment_ids and not missing_studio_segment_ids,
        }

    def _production_segment_id(self, segment: dict) -> str:
        for key in ("id", "source_turn_id"):
            value = segment.get(key)
            if isinstance(value, str) and value:
                return value
        return "unknown-segment"

    def _non_empty_string(self, value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    def _asset_completed_render_ready(self, asset: Asset | None, asset_type: AssetType) -> bool:
        return (
            asset is not None
            and asset.asset_type == asset_type
            and asset.status == "completed"
            and asset.generation_metadata.get("render_ready") is not False
        )

    def _production_asset_entry(self, asset: Asset, episode: Episode) -> dict:
        metadata = asset.generation_metadata or {}
        return {
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type.value,
            "language": asset.language,
            "status": asset.status,
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat(),
            "source_entity_type": asset.source_entity_type,
            "source_entity_id": asset.source_entity_id,
            "source_turn_id": self._source_turn_id(asset, metadata),
            "source_evidence_refs": self._source_evidence_refs(metadata),
            "storage_uri": asset.storage_uri,
            "mime_type": asset.mime_type,
            "duration_ms": asset.duration_ms,
            "width": asset.width,
            "height": asset.height,
            "fps": asset.fps,
            "checksum": asset.checksum,
            "reproducibility": self._asset_reproducibility(metadata),
            "retry_history": self._asset_retry_history(metadata),
            "manual_edits": self._asset_manual_edits(asset, episode, metadata),
            "approval_state": self._asset_approval_state(asset, episode),
            "generation_metadata": self._compact_asset_generation_metadata(metadata),
        }

    def _compact_asset_generation_metadata(self, metadata: dict) -> dict:
        compact: dict = {}
        for key, value in metadata.items():
            safe_value = self._safe_manifest_payload(value)
            try:
                encoded_size = len(
                    json.dumps(safe_value, sort_keys=True, default=str).encode("utf-8")
                )
            except (TypeError, ValueError):
                encoded_size = 16_385
            if encoded_size <= 16_384:
                compact[key] = safe_value
                continue
            summary = {
                "omitted_from_asset_index": True,
                "encoded_size_bytes": encoded_size,
            }
            if isinstance(safe_value, dict):
                for summary_key in (
                    "id",
                    "schema_version",
                    "status",
                    "checksum",
                ):
                    if safe_value.get(summary_key) is not None:
                        summary[summary_key] = safe_value.get(summary_key)
                summary["entry_count"] = len(safe_value)
            elif isinstance(safe_value, list):
                summary["entry_count"] = len(safe_value)
            compact[key] = summary
        return compact

    def _safe_manifest_payload(self, payload: object) -> object:
        return safe_provider_response_payload(payload)

    def _is_sensitive_manifest_key(self, key: str) -> bool:
        return is_sensitive_provider_response_key(key)

    def _source_turn_id(self, asset: Asset, metadata: dict) -> str | None:
        for key in (
            "source_turn_id",
            "transcript_turn_id",
            "discussion_turn_id",
            "canonical_turn_id",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
        if asset.source_entity_type in {
            "transcript_turn",
            "discussion_turn",
            "canonical_turn",
        }:
            return asset.source_entity_id
        return None

    def _source_evidence_refs(self, metadata: dict) -> list[str]:
        refs = metadata.get("evidence_refs")
        if isinstance(refs, list):
            return [str(ref) for ref in refs if ref]
        lineage = metadata.get("evidence_lineage")
        if isinstance(lineage, dict):
            refs = lineage.get("referenced_source_ids")
            if isinstance(refs, list):
                return [str(ref) for ref in refs if ref]
        render_manifest = metadata.get("render_manifest")
        if isinstance(render_manifest, dict):
            lineage = render_manifest.get("evidence_lineage")
            if isinstance(lineage, dict):
                refs = lineage.get("referenced_source_ids")
                if isinstance(refs, list):
                    return [str(ref) for ref in refs if ref]
        return []

    def _asset_reproducibility(self, metadata: dict) -> dict:
        return {
            "model_provider": metadata.get("provider_type"),
            "model_endpoint_id": metadata.get("model_endpoint_id"),
            "model_id": metadata.get("model_id"),
            "model_parameters": metadata.get("sampling"),
            "prompt_template_id": metadata.get("prompt_template_id"),
            "prompt_template_version": metadata.get("prompt_template_version"),
            "workflow_version": metadata.get("workflow_version")
            or metadata.get("workflow_schema_version")
            or metadata.get("schema_version")
            or self._nested_string(metadata, "render_manifest", "schema_version"),
            "voice_id": metadata.get("voice_id") or metadata.get("voice_profile_id"),
            "voicebox_endpoint_id": metadata.get("voicebox_endpoint_id"),
            "comfyui_workflow_id": metadata.get("comfyui_workflow_id"),
            "comfyui_workflow_version": metadata.get("workflow_version")
            or metadata.get("comfyui_workflow_version"),
            "seed": self._asset_seed(metadata),
        }

    def _nested_string(self, metadata: dict, outer_key: str, inner_key: str) -> str | None:
        nested = metadata.get(outer_key)
        if not isinstance(nested, dict):
            return None
        value = nested.get(inner_key)
        return value if isinstance(value, str) and value else None

    def _asset_seed(self, metadata: dict) -> object:
        if metadata.get("seed") is not None:
            return metadata.get("seed")
        prompt_inputs = metadata.get("prompt_inputs")
        if isinstance(prompt_inputs, dict):
            return prompt_inputs.get("seed")
        resolved_prompt_inputs = metadata.get("resolved_prompt_inputs")
        if isinstance(resolved_prompt_inputs, dict):
            return resolved_prompt_inputs.get("seed")
        return None

    def _asset_retry_history(self, metadata: dict) -> dict:
        return {
            "generation_attempt_count": int(metadata.get("generation_attempt_count") or 0),
            "sync_attempt_count": int(metadata.get("sync_attempt_count") or 0),
            "cancellation_attempt_count": int(metadata.get("cancellation_attempt_count") or 0),
            "failure": metadata.get("failure"),
            "failed_at": metadata.get("failed_at"),
            "last_sync_error": metadata.get("last_sync_error"),
            "last_synced_at": metadata.get("last_synced_at"),
            "ready_for_retry": metadata.get("ready_for_retry") is True,
        }

    def _asset_manual_edits(self, asset: Asset, episode: Episode, metadata: dict) -> list[dict]:
        edits: list[dict] = []
        if metadata.get("manual_replacement") is True:
            edits.append(
                {
                    "edit_type": "manual_replacement",
                    "asset_id": str(asset.id),
                    "replacement_of_asset_id": metadata.get("replacement_of_asset_id"),
                    "replaced_by_asset_id": metadata.get("replaced_by_asset_id"),
                    "user_id": metadata.get("user_id"),
                    "comment": metadata.get("comment"),
                }
            )
        for event in episode.audit_events:
            if event.event_type not in {
                "asset.replaced",
                "asset.manual_replacement",
                "timeline.asset_replacement.applied",
            }:
                continue
            details = event.details or {}
            referenced_ids = {
                str(details.get("asset_id") or ""),
                str(details.get("replacement_asset_id") or ""),
                str(details.get("replacement_of_asset_id") or ""),
                str(details.get("replaced_asset_id") or ""),
            }
            if str(asset.id) not in referenced_ids:
                continue
            edits.append(
                {
                    "edit_type": event.event_type,
                    "audit_event_id": str(event.id),
                    "actor": event.actor,
                    "created_at": event.created_at.isoformat(),
                    "details": self._safe_manifest_payload(details),
                }
            )
        return edits

    def _asset_approval_state(self, asset: Asset, episode: Episode) -> dict:
        approvals = [
            approval
            for approval in episode.approvals
            if approval.target_type in {"asset", f"{asset.asset_type.value}_asset"}
            and approval.target_id == str(asset.id)
        ]
        latest = max(approvals, key=lambda approval: approval.created_at, default=None)
        return {
            "decision": latest.decision if latest else None,
            "approval_id": str(latest.id) if latest else None,
            "user_id": latest.user_id if latest else None,
            "created_at": latest.created_at.isoformat() if latest else None,
            "approval_count": len(approvals),
        }

    def _probe_render(self, path: Path) -> dict:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return {
                "probe_tool": "none",
                "probe_warnings": ["ffprobe not available for render probe"],
            }
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size:stream="
                "codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,duration"
            ),
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            payload = json.loads(completed.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            return {
                "probe_tool": "ffprobe",
                "probe_warnings": [f"ffprobe failed: {exc}"],
            }
        streams = payload.get("streams", [])
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            {},
        )
        audio_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            {},
        )
        duration = self._optional_float(payload.get("format", {}).get("duration"))
        video_duration = self._optional_float(video_stream.get("duration"))
        audio_duration = self._optional_float(audio_stream.get("duration"))
        video_duration_ms = int(video_duration * 1000) if video_duration is not None else None
        audio_duration_ms = int(audio_duration * 1000) if audio_duration is not None else None
        return {
            "probe_tool": "ffprobe",
            "probe_warnings": [],
            "duration_ms": int(duration * 1000) if duration is not None else None,
            "video_duration_ms": video_duration_ms,
            "audio_duration_ms": audio_duration_ms,
            "av_offset_ms": (
                abs(video_duration_ms - audio_duration_ms)
                if video_duration_ms is not None and audio_duration_ms is not None
                else None
            ),
            "size_bytes": self._optional_int(payload.get("format", {}).get("size")),
            "width": self._optional_int(video_stream.get("width")),
            "height": self._optional_int(video_stream.get("height")),
            "fps": self._fps_from_rate(video_stream.get("r_frame_rate")),
            "video_codec": video_stream.get("codec_name"),
            "audio_codec": audio_stream.get("codec_name"),
            "audio_sample_rate": self._optional_int(audio_stream.get("sample_rate")),
            "audio_channels": self._optional_int(audio_stream.get("channels")),
        }

    def _render_qc(
        self,
        episode: Episode,
        render_asset: Asset,
        timeline: dict,
        preset: RenderPreset,
        probe: dict,
    ) -> QualityResult:
        issues: list[dict] = []
        if not render_asset.storage_uri:
            issues.append({"severity": "fail", "issue": "render_missing_storage"})
        if not render_asset.checksum:
            issues.append({"severity": "fail", "issue": "render_missing_checksum"})
        for warning in probe.get("probe_warnings", []):
            issues.append(
                {
                    "severity": "warning",
                    "issue": "render_probe_warning",
                    "warning": warning,
                }
            )
        if probe.get("width") != preset.width or probe.get("height") != preset.height:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_dimension_mismatch",
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "expected_width": preset.width,
                    "expected_height": preset.height,
                }
            )
        if probe.get("fps") is None or abs(float(probe["fps"]) - preset.fps) > 0.05:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "render_fps_mismatch",
                    "fps": probe.get("fps"),
                    "expected_fps": preset.fps,
                }
            )
        duration_ms = probe.get("duration_ms")
        expected_duration_ms = int(timeline.get("duration_ms") or 0)
        render_type = render_asset.generation_metadata["render_type"]
        rendered_expected_duration_ms = max(expected_duration_ms, 1000)
        timing_tolerance_ms = max(1, int((1000 / preset.fps) + 0.999))
        composition_policy = render_asset.generation_metadata.get("composition_policy")
        if duration_ms is None:
            issues.append({"severity": "fail", "issue": "render_duration_missing"})
        elif abs(int(duration_ms) - rendered_expected_duration_ms) > 750:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "render_duration_mismatch",
                    "duration_ms": duration_ms,
                    "expected_duration_ms": rendered_expected_duration_ms,
                }
            )
        if (
            composition_policy in {"studio_camera_cuts.v1", "seated_studio_panel.v1"}
            and duration_ms is not None
        ):
            if abs(int(duration_ms) - rendered_expected_duration_ms) > timing_tolerance_ms:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "render_timeline_frame_schedule_mismatch",
                        "duration_ms": duration_ms,
                        "expected_duration_ms": rendered_expected_duration_ms,
                        "tolerance_ms": timing_tolerance_ms,
                    }
                )
            av_offset_ms = probe.get("av_offset_ms")
            if av_offset_ms is None or int(av_offset_ms) > timing_tolerance_ms:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "render_audio_video_sync_mismatch",
                        "video_duration_ms": probe.get("video_duration_ms"),
                        "audio_duration_ms": probe.get("audio_duration_ms"),
                        "av_offset_ms": av_offset_ms,
                        "tolerance_ms": timing_tolerance_ms,
                    }
                )
        episode_minimum_duration_ms = int(episode.minimum_duration_seconds * 1000)
        discussion_maximum_duration_ms = int(episode.maximum_duration_seconds * 1000)
        topic_primer_duration_ms = sum(
            int(segment.get("duration_ms") or 0)
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict) and segment.get("segment_type") == "topic_primer"
        )
        episode_maximum_duration_ms = discussion_maximum_duration_ms + topic_primer_duration_ms
        target_duration_ms = int(episode.target_duration_seconds * 1000)
        final_runtime_within_episode_bounds = None
        if render_type == "final" and duration_ms is not None:
            final_runtime_within_episode_bounds = (
                episode_minimum_duration_ms <= int(duration_ms) <= episode_maximum_duration_ms
            )
            if int(duration_ms) < episode_minimum_duration_ms:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "final_render_runtime_below_minimum",
                        "duration_ms": duration_ms,
                        "minimum_duration_ms": episode_minimum_duration_ms,
                        "target_duration_ms": target_duration_ms,
                    }
                )
            elif int(duration_ms) > episode_maximum_duration_ms:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "final_render_runtime_above_maximum",
                        "duration_ms": duration_ms,
                        "maximum_duration_ms": episode_maximum_duration_ms,
                        "target_duration_ms": target_duration_ms,
                    }
                )
        if probe.get("audio_sample_rate") != preset.audio_sample_rate:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "render_audio_sample_rate_mismatch",
                    "audio_sample_rate": probe.get("audio_sample_rate"),
                    "expected_audio_sample_rate": preset.audio_sample_rate,
                }
            )
        render_manifest = render_asset.generation_metadata.get("render_manifest", {})
        evidence_lineage = (
            render_manifest.get("evidence_lineage", {}) if isinstance(render_manifest, dict) else {}
        )
        evidence_citation_count = int(evidence_lineage.get("citation_count") or 0)
        unresolved_evidence_ref_count = len(
            evidence_lineage.get("unresolved_source_ids", [])
            if isinstance(evidence_lineage.get("unresolved_source_ids"), list)
            else []
        )
        if evidence_citation_count and not evidence_lineage.get("evidence_pack_asset_id"):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_manifest_missing_evidence_pack_lineage",
                }
            )
        if unresolved_evidence_ref_count:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_manifest_unresolved_evidence_refs",
                    "unresolved_source_ids": evidence_lineage.get("unresolved_source_ids", []),
                }
            )
        composition = (
            render_manifest.get("composition", {}) if isinstance(render_manifest, dict) else {}
        )
        stale_source_asset_count = 0
        missing_source_asset_count = 0
        if isinstance(render_manifest, dict):
            source_issues = self._render_manifest_source_asset_issues(
                episode,
                render_manifest,
            )
            stale_source_asset_count = sum(
                1 for issue in source_issues if issue["issue"] == "render_source_asset_stale"
            )
            missing_source_asset_count = sum(
                1 for issue in source_issues if issue["issue"] == "render_source_asset_missing"
            )
            issues.extend(source_issues)
        composition_segment_count = int(composition.get("segment_count") or 0)
        citation_overlay_asset_count = int(composition.get("citation_overlay_asset_count") or 0)
        composited_citation_overlay_count = int(
            composition.get("composited_citation_overlay_count") or 0
        )
        subtitle_track_count = int(composition.get("subtitle_track_count") or 0)
        burned_in_caption_cue_count = int(composition.get("burned_in_caption_cue_count") or 0)
        subtitle_mode = str(timeline.get("media", {}).get("subtitle_mode") or "off")
        if composition_segment_count == 0 and timeline.get("segments"):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_composition_missing_segments",
                }
            )
        if citation_overlay_asset_count or composited_citation_overlay_count:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_composition_contains_evidence_overlays",
                    "citation_overlay_asset_count": citation_overlay_asset_count,
                    "composited_citation_overlay_count": composited_citation_overlay_count,
                }
            )
        if (
            subtitle_mode == "burned_in"
            and subtitle_track_count
            and burned_in_caption_cue_count == 0
        ):
            issues.append(
                {
                    "severity": "warning",
                    "issue": "render_composition_missing_caption_cues",
                    "subtitle_track_count": subtitle_track_count,
                }
            )
        if subtitle_mode == "selectable" and not render_asset.generation_metadata.get(
            "caption_track_asset_id"
        ):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_selectable_caption_track_missing",
                }
            )
        if subtitle_mode == "off" and burned_in_caption_cue_count:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "render_captions_present_while_disabled",
                }
            )
        if composition_policy == "studio_camera_cuts.v1":
            if int(composition.get("studio_context_segment_count") or 0) == 0:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "render_studio_context_missing",
                    }
                )
            if (
                any(
                    isinstance(segment, dict) and segment.get("segment_type") == "topic_primer"
                    for segment in timeline.get("segments", [])
                )
                and int(composition.get("post_primer_host_bridge_segment_count") or 0) == 0
            ):
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "render_post_primer_host_bridge_missing",
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
            target_type="render_asset",
            target_id=str(render_asset.id),
            check_type=f"render_{render_asset.generation_metadata['render_type']}_integrity",
            severity=severity,
            status=severity.value,
            score=0.0 if failure_count else 1.0,
            details={
                "render_asset_id": str(render_asset.id),
                "render_id": render_asset.generation_metadata["render_id"],
                "render_type": render_type,
                "review_scope": render_asset.generation_metadata.get("review_scope"),
                "preset_id": preset.id,
                "timeline_asset_id": render_asset.generation_metadata["timeline_asset_id"],
                "duration_ms": duration_ms,
                "expected_duration_ms": rendered_expected_duration_ms,
                "timing_tolerance_ms": timing_tolerance_ms,
                "video_duration_ms": probe.get("video_duration_ms"),
                "audio_duration_ms": probe.get("audio_duration_ms"),
                "av_offset_ms": probe.get("av_offset_ms"),
                "composition_policy": composition_policy,
                "target_duration_ms": target_duration_ms,
                "full_program_target_duration_ms": (target_duration_ms + topic_primer_duration_ms),
                "minimum_duration_ms": episode_minimum_duration_ms,
                "maximum_duration_ms": episode_maximum_duration_ms,
                "discussion_maximum_duration_ms": discussion_maximum_duration_ms,
                "topic_primer_duration_ms": topic_primer_duration_ms,
                "final_runtime_within_episode_bounds": final_runtime_within_episode_bounds,
                "width": probe.get("width"),
                "height": probe.get("height"),
                "fps": probe.get("fps"),
                "audio_sample_rate": probe.get("audio_sample_rate"),
                "audio_channels": probe.get("audio_channels"),
                "source_asset_count": len(
                    render_manifest.get("source_assets", [])
                    if isinstance(render_manifest, dict)
                    else []
                ),
                "stale_source_asset_count": stale_source_asset_count,
                "missing_source_asset_count": missing_source_asset_count,
                "composition_mode": composition.get("mode")
                or render_manifest.get("composition_mode"),
                "composition_segment_count": composition_segment_count,
                "studio_context_segment_count": int(
                    composition.get("studio_context_segment_count") or 0
                ),
                "post_primer_host_bridge_segment_count": int(
                    composition.get("post_primer_host_bridge_segment_count") or 0
                ),
                "composited_citation_overlay_count": composited_citation_overlay_count,
                "citation_overlay_asset_count": citation_overlay_asset_count,
                "resolved_citation_overlay_asset_count": int(
                    composition.get("resolved_citation_overlay_asset_count") or 0
                ),
                "resolved_visual_asset_count": int(
                    composition.get("resolved_visual_asset_count") or 0
                ),
                "visual_plate_layer_count": int(composition.get("visual_plate_layer_count") or 0),
                "resolved_visual_plate_layer_count": int(
                    composition.get("resolved_visual_plate_layer_count") or 0
                ),
                "generated_visual_fallback_count": int(
                    composition.get("generated_visual_fallback_count") or 0
                ),
                "composited_visual_overlay_layer_count": int(
                    composition.get("composited_visual_overlay_layer_count") or 0
                ),
                "layout_policy_names": (
                    composition.get("layout_policy_names", [])
                    if isinstance(composition.get("layout_policy_names"), list)
                    else []
                ),
                "transition_policy_names": (
                    composition.get("transition_policy_names", [])
                    if isinstance(composition.get("transition_policy_names"), list)
                    else []
                ),
                "animated_scene_count": int(composition.get("animated_scene_count") or 0),
                "motion_primitive_names": (
                    composition.get("motion_primitive_names", [])
                    if isinstance(composition.get("motion_primitive_names"), list)
                    else []
                ),
                "motion_primitive_count": int(composition.get("motion_primitive_count") or 0),
                "advanced_layout_policy_count": int(
                    composition.get("advanced_layout_policy_count") or 0
                ),
                "split_screen_scene_count": int(composition.get("split_screen_scene_count") or 0),
                "focus_shift_scene_count": int(composition.get("focus_shift_scene_count") or 0),
                "cross_scene_transition_count": int(
                    composition.get("cross_scene_transition_count") or 0
                ),
                "rendered_cross_scene_xfade_count": int(
                    composition.get("rendered_cross_scene_xfade_count") or 0
                ),
                "cross_scene_renderer": composition.get("cross_scene_renderer"),
                "rendered_layer_transform_names": (
                    composition.get("rendered_layer_transform_names", [])
                    if isinstance(composition.get("rendered_layer_transform_names"), list)
                    else []
                ),
                "rendered_layer_transform_count": int(
                    composition.get("rendered_layer_transform_count") or 0
                ),
                "rendered_layer_opacity_keyframe_count": int(
                    composition.get("rendered_layer_opacity_keyframe_count") or 0
                ),
                "rendered_layer_scale_keyframe_count": int(
                    composition.get("rendered_layer_scale_keyframe_count") or 0
                ),
                "rendered_layer_easing_curve_names": (
                    composition.get("rendered_layer_easing_curve_names", [])
                    if isinstance(composition.get("rendered_layer_easing_curve_names"), list)
                    else []
                ),
                "rendered_layer_easing_curve_count": int(
                    composition.get("rendered_layer_easing_curve_count") or 0
                ),
                "rendered_layer_mask_names": (
                    composition.get("rendered_layer_mask_names", [])
                    if isinstance(composition.get("rendered_layer_mask_names"), list)
                    else []
                ),
                "rendered_layer_mask_count": int(composition.get("rendered_layer_mask_count") or 0),
                "rendered_non_rectangular_mask_count": int(
                    composition.get("rendered_non_rectangular_mask_count") or 0
                ),
                "layer_mask_renderer": composition.get("layer_mask_renderer"),
                "layer_motion_renderer": composition.get("layer_motion_renderer"),
                "resolved_audio_asset_count": int(
                    composition.get("resolved_audio_asset_count") or 0
                ),
                "dialogue_audio_layer_count": int(
                    composition.get("dialogue_audio_layer_count") or 0
                ),
                "resolved_dialogue_audio_layer_count": int(
                    composition.get("resolved_dialogue_audio_layer_count") or 0
                ),
                "silent_dialogue_fallback_count": int(
                    composition.get("silent_dialogue_fallback_count") or 0
                ),
                "subtitle_track_count": subtitle_track_count,
                "resolved_subtitle_track_count": int(
                    composition.get("resolved_subtitle_track_count") or 0
                ),
                "burned_in_caption_cue_count": burned_in_caption_cue_count,
                "caption_track_asset_id": render_asset.generation_metadata.get(
                    "caption_track_asset_id"
                ),
                "caption_track_mode": render_asset.generation_metadata.get("caption_track_mode"),
                "evidence_pack_asset_id": evidence_lineage.get("evidence_pack_asset_id"),
                "evidence_citation_count": evidence_citation_count,
                "evidence_source_count": len(
                    evidence_lineage.get("referenced_sources", [])
                    if isinstance(evidence_lineage.get("referenced_sources"), list)
                    else []
                ),
                "unresolved_evidence_ref_count": unresolved_evidence_ref_count,
                "issue_count": len(issues),
                "failure_count": failure_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _render_manifest_source_asset_issues(
        self,
        episode: Episode,
        render_manifest: dict,
    ) -> list[dict]:
        source_assets = render_manifest.get("source_assets", [])
        if not isinstance(source_assets, list):
            return []
        issues: list[dict] = []
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        for source in source_assets:
            if not isinstance(source, dict):
                continue
            asset_id = source.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                continue
            current = asset_by_id.get(asset_id)
            if current is None:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "render_source_asset_missing",
                        "asset_id": asset_id,
                    }
                )
                continue
            mismatch_reasons = self._render_source_asset_mismatch_reasons(source, current)
            if mismatch_reasons:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "render_source_asset_stale",
                        "asset_id": asset_id,
                        "asset_type": source.get("asset_type"),
                        "mismatch_reasons": mismatch_reasons,
                    }
                )
        return issues

    def _render_source_asset_mismatch_reasons(
        self,
        source: dict,
        current: Asset,
    ) -> list[str]:
        checks = {
            "asset_type": current.asset_type.value,
            "source_entity_type": current.source_entity_type,
            "source_entity_id": current.source_entity_id,
            "status": current.status,
            "storage_uri": current.storage_uri,
            "mime_type": current.mime_type,
            "duration_ms": current.duration_ms,
            "width": current.width,
            "height": current.height,
            "fps": current.fps,
            "checksum": current.checksum,
            "render_ready": current.generation_metadata.get("render_ready"),
        }
        return [
            f"{key}_changed"
            for key, value in checks.items()
            if key in source and source.get(key) != value
        ]

    def _thumbnail_qc(
        self,
        episode: Episode,
        thumbnail_asset: Asset,
        render_asset: Asset,
        probe: dict,
        *,
        average_luma: int | None = None,
    ) -> QualityResult:
        issues: list[dict] = []
        if not thumbnail_asset.storage_uri:
            issues.append({"severity": "fail", "issue": "thumbnail_missing_storage"})
        if not thumbnail_asset.checksum:
            issues.append({"severity": "fail", "issue": "thumbnail_missing_checksum"})
        for warning in probe.get("probe_warnings", []):
            issues.append(
                {
                    "severity": "warning",
                    "issue": "thumbnail_probe_warning",
                    "warning": warning,
                }
            )
        if not probe.get("width") or not probe.get("height"):
            issues.append({"severity": "fail", "issue": "thumbnail_dimensions_missing"})
        elif (
            render_asset.width
            and render_asset.height
            and (
                probe.get("width") != render_asset.width
                or probe.get("height") != render_asset.height
            )
        ):
            issues.append(
                {
                    "severity": "warning",
                    "issue": "thumbnail_render_dimension_mismatch",
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "render_width": render_asset.width,
                    "render_height": render_asset.height,
                }
            )
        if average_luma is None:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "thumbnail_luma_unavailable",
                }
            )
        elif average_luma < 8:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "thumbnail_near_black",
                    "average_luma": average_luma,
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
            target_type="thumbnail_asset",
            target_id=str(thumbnail_asset.id),
            check_type="thumbnail_integrity",
            severity=severity,
            status=severity.value,
            score=0.0 if failure_count else 1.0,
            details={
                "thumbnail_asset_id": str(thumbnail_asset.id),
                "render_asset_id": str(render_asset.id),
                "width": probe.get("width"),
                "height": probe.get("height"),
                "average_luma": average_luma,
                "issue_count": len(issues),
                "failure_count": failure_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _youtube_package_qc(
        self,
        episode: Episode,
        package_asset: Asset,
        render_asset: Asset,
        thumbnail_asset: Asset | None,
        included_files: list[str],
    ) -> QualityResult:
        issues: list[dict] = []
        if not package_asset.storage_uri:
            issues.append({"severity": "fail", "issue": "youtube_package_missing_storage"})
        if not package_asset.checksum:
            issues.append({"severity": "fail", "issue": "youtube_package_missing_checksum"})
        if render_asset.generation_metadata.get("render_type") != "final":
            issues.append(
                {
                    "severity": "warning",
                    "issue": "youtube_package_uses_non_final_render",
                    "render_type": render_asset.generation_metadata.get("render_type"),
                }
            )
        if "video/render.mp4" not in included_files:
            issues.append({"severity": "fail", "issue": "youtube_package_missing_video_file"})
        thumbnail_required = thumbnail_asset is not None
        if thumbnail_asset is None:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "youtube_package_missing_thumbnail_asset",
                }
            )
        elif "thumbnail/thumbnail.jpg" not in included_files:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "youtube_package_missing_required_thumbnail",
                    "thumbnail_asset_id": str(thumbnail_asset.id),
                }
            )
        if "youtube-package.json" not in included_files:
            issues.append({"severity": "fail", "issue": "youtube_package_missing_manifest"})
        subtitle_count = sum(1 for name in included_files if name.startswith("subtitles/"))
        required_subtitle_assets = self._subtitle_assets_for_package(episode, render_asset)
        required_subtitle_asset_count = len(required_subtitle_assets)
        if required_subtitle_asset_count and subtitle_count == 0:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "youtube_package_missing_required_subtitles",
                    "required_subtitle_asset_count": required_subtitle_asset_count,
                    "required_subtitle_asset_ids": [
                        str(asset.id) for asset in required_subtitle_assets
                    ],
                }
            )
        elif subtitle_count == 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "youtube_package_missing_subtitles",
                }
            )
        package_manifest = package_asset.generation_metadata.get(
            "youtube_package_manifest",
            {},
        )
        evidence_lineage = (
            package_manifest.get("evidence_lineage", {})
            if isinstance(package_manifest, dict)
            else {}
        )
        chapters = (
            package_manifest.get("chapters", []) if isinstance(package_manifest, dict) else []
        )
        chapter_count = len(chapters) if isinstance(chapters, list) else 0
        expected_chapters = self._timeline_chapters(
            self._timeline_from_render_asset(episode, render_asset)
        )
        if expected_chapters and not self._chapters_match(expected_chapters, chapters):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "youtube_package_missing_required_chapters",
                    "required_chapter_count": len(expected_chapters),
                    "package_chapter_count": chapter_count,
                }
            )
        evidence_citation_count = int(evidence_lineage.get("citation_count") or 0)
        unresolved_evidence_ref_count = len(
            evidence_lineage.get("unresolved_source_ids", [])
            if isinstance(evidence_lineage.get("unresolved_source_ids"), list)
            else []
        )
        if evidence_citation_count and not evidence_lineage.get("evidence_pack_asset_id"):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "youtube_package_missing_evidence_pack_lineage",
                }
            )
        if unresolved_evidence_ref_count:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "youtube_package_unresolved_evidence_refs",
                    "unresolved_source_ids": evidence_lineage.get("unresolved_source_ids", []),
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
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=severity,
            status=severity.value,
            score=0.0 if failure_count else 1.0,
            details={
                "package_asset_id": str(package_asset.id),
                "package_id": package_asset.generation_metadata["package_id"],
                "render_asset_id": str(render_asset.id),
                "thumbnail_asset_id": str(thumbnail_asset.id) if thumbnail_asset else None,
                "thumbnail_required": thumbnail_required,
                "included_file_count": len(included_files),
                "subtitle_file_count": subtitle_count,
                "chapter_count": chapter_count,
                "required_subtitle_asset_count": required_subtitle_asset_count,
                "evidence_pack_asset_id": evidence_lineage.get("evidence_pack_asset_id"),
                "evidence_citation_count": evidence_citation_count,
                "evidence_source_count": len(
                    evidence_lineage.get("referenced_sources", [])
                    if isinstance(evidence_lineage.get("referenced_sources"), list)
                    else []
                ),
                "unresolved_evidence_ref_count": unresolved_evidence_ref_count,
                "included_files": included_files,
                "issue_count": len(issues),
                "failure_count": failure_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _preset_by_id(self, presets: list[RenderPreset], preset_id: str) -> RenderPreset:
        preset = next((item for item in presets if item.id == preset_id and item.enabled), None)
        if preset is None:
            raise ValueError(f"unknown render preset {preset_id}")
        return preset

    def _target_timeline_asset(self, episode: Episode, request: RenderRequest) -> Asset:
        if request.timeline_asset_id is not None:
            asset = next(
                (item for item in episode.assets if item.id == request.timeline_asset_id),
                None,
            )
            if asset is None or asset.asset_type != AssetType.timeline:
                raise ValueError("timeline asset not found")
            if asset.status != "completed":
                raise ValueError("timeline asset is not completed")
            return asset
        return (
            next(
                (
                    asset
                    for asset in reversed(episode.assets)
                    if asset.asset_type == AssetType.timeline
                    and asset.status == "completed"
                    and (
                        request.transcript_version_id is None
                        or asset.source_entity_id == str(request.transcript_version_id)
                    )
                    and (request.language is None or asset.language == request.language)
                ),
                None,
            )
            or self._raise_timeline_missing()
        )

    def _raise_timeline_missing(self) -> Asset:
        raise ValueError("timeline not found")

    def _target_render_asset(
        self,
        episode: Episode,
        render_asset_id: object | None,
        prefer_final: bool = False,
        allow_preview: bool = True,
    ) -> Asset:
        if render_asset_id is not None:
            asset = next((item for item in episode.assets if item.id == render_asset_id), None)
            if asset is None or asset.asset_type != AssetType.render:
                raise ValueError("render asset not found")
            if asset.status != "completed":
                raise ValueError("render asset is not completed")
            return asset
        candidates = [
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.render and asset.status == "completed"
        ]
        if prefer_final:
            final = [
                asset
                for asset in candidates
                if asset.generation_metadata.get("render_type") == "final"
            ]
            if final:
                return final[-1]
            if not allow_preview:
                raise ValueError("final render not found")
        if not candidates:
            raise ValueError("render asset not found")
        return candidates[-1]

    def _final_render_approved(self, episode: Episode, render_asset: Asset) -> bool:
        if render_asset.generation_metadata.get("approval_status") == "approved":
            return True
        return any(
            approval.stage == "final_render_review"
            and approval.target_type == "render_asset"
            and approval.target_id == str(render_asset.id)
            and approval.decision == "approved"
            for approval in episode.approvals
        )

    def _ensure_preview_approved(self, episode: Episode, timeline_asset: Asset) -> None:
        preview = self._latest_render_asset_for_timeline(
            episode,
            timeline_asset,
            render_type="preview",
        )
        if preview is None:
            raise ValueError("approved preview render is required before final rendering")
        if self._preview_render_approved(episode, preview):
            return
        raise ValueError("approved preview render is required before final rendering")

    def _preview_render_approved(self, episode: Episode, render_asset: Asset) -> bool:
        if render_asset.generation_metadata.get("approval_status") == "approved":
            return True
        return any(
            approval.stage == "preview_render_review"
            and approval.target_type == "render_asset"
            and approval.target_id == str(render_asset.id)
            and approval.decision == "approved"
            for approval in episode.approvals
        )

    def _latest_render_asset_for_timeline(
        self,
        episode: Episode,
        timeline_asset: Asset,
        render_type: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.render
                and asset.source_entity_type == "timeline_asset"
                and asset.source_entity_id == str(timeline_asset.id)
                and asset.status == "completed"
                and asset.generation_metadata.get("render_type") == render_type
            ),
            None,
        )

    def _target_thumbnail_asset(
        self,
        episode: Episode,
        thumbnail_asset_id: object | None,
        render_asset: Asset,
    ) -> Asset | None:
        if thumbnail_asset_id is not None:
            asset = next((item for item in episode.assets if item.id == thumbnail_asset_id), None)
            if asset is None or asset.asset_type != AssetType.thumbnail:
                raise ValueError("thumbnail asset not found")
            if asset.status != "completed":
                raise ValueError("thumbnail asset is not completed")
            return asset
        return self._latest_thumbnail_asset(episode, render_asset)

    def _latest_thumbnail_asset(self, episode: Episode, render_asset: Asset) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.thumbnail
                and asset.source_entity_type == "render_asset"
                and asset.source_entity_id == str(render_asset.id)
                and asset.status == "completed"
            ),
            None,
        )

    def _latest_export_package_asset(self, episode: Episode, render_asset: Asset) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.export_package
                and asset.source_entity_type == "render_asset"
                and asset.source_entity_id == str(render_asset.id)
                and asset.status == "completed"
            ),
            None,
        )

    def _target_export_package_asset(
        self,
        episode: Episode,
        package_asset_id: object | None,
    ) -> Asset:
        if package_asset_id is not None:
            asset = next((item for item in episode.assets if item.id == package_asset_id), None)
            if asset is None or asset.asset_type != AssetType.export_package:
                raise ValueError("export package asset not found")
            if asset.status != "completed":
                raise ValueError("export package asset is not completed")
            return asset
        package_asset = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.export_package and asset.status == "completed"
            ),
            None,
        )
        if package_asset is None:
            raise ValueError("export package asset not found")
        return package_asset

    def _target_manifest_render_asset(
        self,
        episode: Episode,
        package_asset: Asset,
        render_asset_id: object | None,
    ) -> Asset:
        if render_asset_id is not None:
            return self._target_render_asset(episode, render_asset_id)
        render_asset = next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.render
                and str(asset.id) == package_asset.source_entity_id
                and asset.status == "completed"
            ),
            None,
        )
        if render_asset is None:
            raise ValueError("render asset for export package not found")
        return render_asset

    def _ensure_package_qc_allows_manifest(self, episode: Episode, package_asset: Asset) -> None:
        package_qc = self._latest_youtube_package_qc(episode, package_asset)
        if package_qc is None:
            raise ValueError("YouTube package QC is required before production manifest generation")
        if package_qc.status == "fail" or package_qc.severity == QualitySeverity.fail:
            raise ValueError("failing YouTube package QC blocks production manifest generation")

    def _latest_youtube_package_qc(
        self,
        episode: Episode,
        package_asset: Asset,
    ) -> QualityResult | None:
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "youtube_package_integrity"
                and result.target_id == str(package_asset.id)
            ),
            None,
        )

    def _latest_production_manifest_asset(
        self,
        episode: Episode,
        package_asset: Asset,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.production_manifest
                and asset.source_entity_type == "export_package"
                and asset.source_entity_id == str(package_asset.id)
                and asset.status == "completed"
            ),
            None,
        )

    def _timeline_json(self, timeline_asset: Asset) -> dict:
        timeline = timeline_asset.generation_metadata.get("timeline_json")
        if isinstance(timeline, dict):
            return timeline
        if timeline_asset.storage_uri is None:
            raise ValueError("timeline asset has no storage URI")
        path = self.object_store.path_for_uri(timeline_asset.storage_uri)
        if path is None or not path.exists():
            raise ValueError("timeline object not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("timeline object is not a JSON object")
        return payload

    def _latest_evidence_pack_asset(self, episode: Episode) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
            ),
            None,
        )

    def _evidence_pack_json(self, evidence_pack_asset: Asset) -> dict:
        pack = evidence_pack_asset.generation_metadata.get("evidence_pack")
        if isinstance(pack, dict):
            return pack
        if evidence_pack_asset.storage_uri is None:
            return {}
        path = self.object_store.path_for_uri(evidence_pack_asset.storage_uri)
        if path is None or not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _timeline_from_render_asset(self, episode: Episode, render_asset: Asset) -> dict:
        timeline_asset_id = render_asset.generation_metadata.get("timeline_asset_id")
        timeline_asset = next(
            (
                asset
                for asset in episode.assets
                if str(asset.id) == str(timeline_asset_id)
                and asset.asset_type == AssetType.timeline
            ),
            None,
        )
        if timeline_asset is None:
            return {}
        return self._timeline_json(timeline_asset)

    def _path_for_asset(self, asset: Asset) -> Path:
        metadata_path = asset.generation_metadata.get("object_storage_path")
        if isinstance(metadata_path, str) and Path(metadata_path).exists():
            return Path(metadata_path)
        if asset.storage_uri is None:
            raise ValueError(f"{asset.asset_type.value} asset has no storage URI")
        path = self.object_store.path_for_uri(asset.storage_uri)
        if path is None or not path.exists():
            raise ValueError(f"{asset.asset_type.value} object not found")
        return path

    def _optional_path_for_asset(self, asset: Asset | None) -> Path | None:
        if asset is None:
            return None
        metadata_path = asset.generation_metadata.get("object_storage_path")
        if isinstance(metadata_path, str) and Path(metadata_path).exists():
            return Path(metadata_path)
        if asset.storage_uri is None:
            return None
        path = self.object_store.path_for_uri(asset.storage_uri)
        if path is None or not path.exists():
            return None
        return path

    def _optional_path_for_storage_uri(self, storage_uri: object) -> Path | None:
        if not isinstance(storage_uri, str) or not storage_uri:
            return None
        path = self.object_store.path_for_uri(storage_uri)
        if path is None or not path.exists():
            return None
        return path

    @staticmethod
    def _mime_type_for_path(path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
        }.get(suffix, "application/octet-stream")

    def _mime_type_for_storage_uri(self, storage_uri: str) -> str:
        path = self._optional_path_for_storage_uri(storage_uri)
        if path is not None:
            return self._mime_type_for_path(path)
        return self._mime_type_for_path(Path(storage_uri))

    def _asset_path_exists(self, asset: Asset | None) -> bool:
        if asset is None:
            return False
        metadata_path = asset.generation_metadata.get("object_storage_path")
        if isinstance(metadata_path, str) and Path(metadata_path).exists():
            return True
        if asset.storage_uri is None:
            return False
        path = self.object_store.path_for_uri(asset.storage_uri)
        return path is not None and path.exists()

    def _between_enable(self, start_seconds: float, end_seconds: float) -> str:
        return f"between(t\\,{start_seconds:.3f}\\,{end_seconds:.3f})"

    def _audio_channel_count(self, audio_layout: str) -> int:
        if audio_layout == "mono":
            return 1
        if audio_layout == "stereo":
            return 2
        if audio_layout.startswith("5.1"):
            return 6
        if audio_layout.startswith("7.1"):
            return 8
        return 2

    def _scene_color(self, index: int) -> str:
        palette = [
            "0x102a43",
            "0x1f2937",
            "0x223b53",
            "0x273449",
            "0x172554",
            "0x064e3b",
        ]
        return palette[index % len(palette)]

    def _caption_lines(self, text: str, max_chars: int = 54) -> list[str]:
        words = text.split()
        if not words:
            return []
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > max_chars:
                lines.append(current)
                current = word
                if len(lines) == 2:
                    break
            else:
                current = candidate
        if len(lines) < 2 and current:
            lines.append(current)
        if len(lines) > 2:
            lines = lines[:2]
        if len(lines) == 2 and len(" ".join(words)) > len(" ".join(lines)):
            lines[-1] = lines[-1].rstrip(". ") + "..."
        return lines

    def _ffmpeg_text(self, value: str) -> str:
        text = " ".join(value.split())
        replacements = {
            "\\": "\\\\",
            ":": "\\:",
            "'": "\u2019",
            "%": "\\%",
            "[": "\\[",
            "]": "\\]",
            ",": "\\,",
        }
        return "".join(replacements.get(character, character) for character in text)

    def _extension_for_asset(self, asset: Asset) -> str:
        mime_type = asset.mime_type or ""
        if mime_type == "video/mp4":
            return "mp4"
        if mime_type == "image/jpeg":
            return "jpg"
        if mime_type == "text/vtt":
            return "vtt"
        if mime_type == "application/x-subrip":
            return "srt"
        return "bin"

    def _subtitle_assets_for_package(self, episode: Episode, render_asset: Asset) -> list[Asset]:
        manifest = render_asset.generation_metadata.get("render_manifest", {})
        source_asset_ids = {
            item.get("asset_id")
            for item in manifest.get("source_assets", [])
            if isinstance(item, dict) and item.get("asset_type") == AssetType.subtitle.value
        }
        return [
            asset
            for asset in episode.assets
            if str(asset.id) in source_asset_ids and asset.asset_type == AssetType.subtitle
        ]

    def _subtitle_entries(self, episode: Episode, render_manifest: dict) -> list[dict]:
        source_asset_ids = {
            item.get("asset_id")
            for item in render_manifest.get("source_assets", [])
            if isinstance(item, dict) and item.get("asset_type") == AssetType.subtitle.value
        }
        entries = []
        for asset in episode.assets:
            if str(asset.id) not in source_asset_ids or asset.asset_type != AssetType.subtitle:
                continue
            entries.append(
                {
                    "asset_id": str(asset.id),
                    "language": asset.language,
                    "format": asset.generation_metadata.get("format"),
                    "storage_uri": asset.storage_uri,
                    "checksum": asset.checksum,
                }
            )
        return entries

    def _subtitle_filename(self, asset: Asset) -> str:
        subtitle_format = asset.generation_metadata.get("format")
        extension = (
            subtitle_format
            if isinstance(subtitle_format, str)
            else self._extension_for_asset(asset)
        )
        language = asset.language or "und"
        return f"subtitles/{language}.{extension}"

    def _youtube_description(self, episode: Episode, chapters: list[dict]) -> str:
        lines = [
            episode.definition.topic.central_question,
            "",
            "Chapters:",
        ]
        if chapters:
            lines.extend(f"{chapter['start_time']} {chapter['title']}" for chapter in chapters)
        else:
            lines.append("00:00 Episode")
        return "\n".join(lines)

    def _timeline_chapters(self, timeline: dict) -> list[dict]:
        chapters: list[dict] = []
        for index, chapter in enumerate(timeline.get("chapters", []), start=1):
            if not isinstance(chapter, dict):
                continue
            start_ms = self._optional_int(chapter.get("start_ms"))
            if start_ms is None:
                start_ms = 0
            entry = {
                "title": chapter.get("title") or f"Chapter {index}",
                "start_ms": max(0, start_ms),
                "start_time": self._chapter_timecode(max(0, start_ms)),
                "source_turn_id": chapter.get("source_turn_id"),
                "segment_id": chapter.get("segment_id"),
            }
            chapters.append(entry)
        return chapters

    def _chapters_match(self, expected: list[dict], actual: object) -> bool:
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        actual_by_start = {
            int(chapter.get("start_ms") or 0): chapter
            for chapter in actual
            if isinstance(chapter, dict)
        }
        for chapter in expected:
            start_ms = int(chapter.get("start_ms") or 0)
            actual_chapter = actual_by_start.get(start_ms)
            if actual_chapter is None:
                return False
            if str(actual_chapter.get("title") or "") != str(chapter.get("title") or ""):
                return False
        return True

    def _chapter_timecode(self, start_ms: int) -> str:
        total_seconds = max(0, start_ms // 1000)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _latest_render_asset(
        self,
        episode: Episode,
        timeline_asset: Asset,
        request: RenderRequest,
        preset: RenderPreset,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.render
                and asset.source_entity_type == "timeline_asset"
                and asset.source_entity_id == str(timeline_asset.id)
                and asset.status == "completed"
                and asset.generation_metadata.get("render_type") == request.render_type
                and asset.generation_metadata.get("preset_id") == preset.id
            ),
            None,
        )

    def _active_render_asset(
        self,
        episode: Episode,
        timeline_asset: Asset,
        request: RenderRequest,
        preset: RenderPreset,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.render
                and asset.source_entity_type == "timeline_asset"
                and asset.source_entity_id == str(timeline_asset.id)
                and asset.status in {"submitted", "running", "completed"}
                and asset.generation_metadata.get("render_type") == request.render_type
                and asset.generation_metadata.get("preset_id") == preset.id
            ),
            None,
        )

    def _queued_render_asset(
        self,
        episode: Episode,
        render_asset_id: UUID | None,
        timeline_asset: Asset,
        request: RenderRequest,
        preset: RenderPreset,
    ) -> Asset | None:
        if render_asset_id is None:
            return None
        asset = self._render_asset_by_id(episode, render_asset_id)
        if asset.status not in {"submitted", "running"}:
            raise ValueError("render request is not available for processing")
        if (
            asset.source_entity_type != "timeline_asset"
            or asset.source_entity_id != str(timeline_asset.id)
            or asset.generation_metadata.get("render_type") != request.render_type
            or asset.generation_metadata.get("preset_id") != preset.id
        ):
            raise ValueError("render request does not match target timeline or preset")
        return asset

    @staticmethod
    def _render_request_payload(request: RenderRequest) -> dict:
        return {
            "timeline_asset_id": str(request.timeline_asset_id)
            if request.timeline_asset_id is not None
            else None,
            "transcript_version_id": str(request.transcript_version_id)
            if request.transcript_version_id is not None
            else None,
            "language": request.language,
            "render_type": request.render_type,
            "review_scope": request.review_scope,
            "preset_id": request.preset_id,
            "user_id": request.user_id,
            "regenerate": request.regenerate,
            "allow_unapproved_preview": request.allow_unapproved_preview,
            "allow_paused_episode": request.allow_paused_episode,
        }

    @staticmethod
    def _render_asset_by_id(episode: Episode, render_asset_id: UUID) -> Asset:
        asset = next((item for item in episode.assets if item.id == render_asset_id), None)
        if asset is None or asset.asset_type != AssetType.render:
            raise ValueError("render asset not found")
        return asset

    def _optional_float(self, value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _optional_int(self, value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _fps_from_rate(self, value: object) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        if "/" not in value:
            return self._optional_float(value)
        numerator, denominator = value.split("/", 1)
        numerator_value = self._optional_float(numerator)
        denominator_value = self._optional_float(denominator)
        if numerator_value is None or not denominator_value:
            return None
        return round(numerator_value / denominator_value, 3)
