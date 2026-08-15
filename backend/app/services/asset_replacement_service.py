from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.domain.enums import AssetType
from app.domain.schemas import Asset, AssetReplacementRequest, AuditEvent, Episode
from app.services.object_storage import ObjectStore, create_object_store


class AssetReplacementService:
    timeline_reference_fields = {
        "audio_asset_id",
        "video_asset_id",
        "secondary_visual_asset_id",
        "reaction_visual_asset_id",
        "studio_scene_asset_id",
        "fallback_video_asset_id",
        "subtitle_asset_id",
    }

    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)

    def replace_asset(
        self,
        episode: Episode,
        asset_id: UUID,
        request: AssetReplacementRequest,
    ) -> Episode:
        original = next((asset for asset in episode.assets if asset.id == asset_id), None)
        if original is None:
            raise KeyError("asset not found")
        if original.status == "replaced":
            raise ValueError("asset has already been replaced")
        if original.asset_type == AssetType.timeline:
            raise ValueError("timeline assets must be edited through the timeline endpoint")

        now = datetime.now(UTC)
        replacement = Asset(
            episode_id=episode.id,
            asset_type=original.asset_type,
            language=original.language,
            source_entity_type=original.source_entity_type,
            source_entity_id=original.source_entity_id,
            storage_uri=request.storage_uri,
            mime_type=request.mime_type or original.mime_type,
            duration_ms=(
                request.duration_ms
                if request.duration_ms is not None
                else original.duration_ms
            ),
            width=request.width if request.width is not None else original.width,
            height=request.height if request.height is not None else original.height,
            fps=request.fps if request.fps is not None else original.fps,
            checksum=request.checksum,
            generation_metadata={
                **request.generation_metadata,
                "schema_version": "manual_asset_replacement.v1",
                "manual_replacement": True,
                "replacement_of_asset_id": str(original.id),
                "replacement_reason": request.comment,
                "replaced_at": now.isoformat(),
                "replaced_by": request.user_id or "system",
                "original_asset": {
                    "asset_id": str(original.id),
                    "asset_type": str(original.asset_type),
                    "status": original.status,
                    "storage_uri": original.storage_uri,
                    "checksum": original.checksum,
                },
            },
            status=request.status,
            created_at=now,
            updated_at=now,
        )
        original.status = "replaced"
        original.updated_at = now
        original.generation_metadata = {
            **original.generation_metadata,
            "replaced_by_asset_id": str(replacement.id),
            "replaced_at": now.isoformat(),
            "replacement_reason": request.comment,
        }
        episode.assets.append(replacement)
        timeline_updates = self._rewrite_active_timeline_references(
            episode,
            old_asset_id=str(original.id),
            new_asset_id=str(replacement.id),
            actor=request.user_id or "system",
            comment=request.comment,
            replaced_at=now,
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="asset.replaced",
                actor=request.user_id or "system",
                details={
                    "original_asset_id": str(original.id),
                    "replacement_asset_id": str(replacement.id),
                    "asset_type": str(original.asset_type),
                    "source_entity_type": original.source_entity_type,
                    "source_entity_id": original.source_entity_id,
                    "timeline_asset_updates": timeline_updates,
                    "comment": request.comment,
                },
            )
        )
        episode.updated_at = now
        return episode

    def _rewrite_active_timeline_references(
        self,
        episode: Episode,
        old_asset_id: str,
        new_asset_id: str,
        actor: str,
        comment: str | None,
        replaced_at: datetime,
    ) -> list[dict]:
        updates: list[dict] = []
        for asset in episode.assets:
            if asset.asset_type != AssetType.timeline or asset.status == "replaced":
                continue
            timeline = asset.generation_metadata.get("timeline_json")
            if not isinstance(timeline, dict):
                continue
            replacement_count = self._replace_timeline_asset_ids(
                timeline,
                old_asset_id=old_asset_id,
                new_asset_id=new_asset_id,
            )
            if replacement_count == 0:
                continue
            stored = self.object_store.put_bytes(
                key=f"timelines/{episode.id}/{timeline.get('id')}-asset-replacement.json",
                payload=json.dumps(timeline, indent=2, sort_keys=True).encode("utf-8"),
                content_type="application/vnd.dialecticore.timeline+json",
            )
            asset.storage_uri = stored.uri
            asset.checksum = stored.checksum
            asset.updated_at = replaced_at
            asset.generation_metadata = {
                **asset.generation_metadata,
                "timeline_json": timeline,
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
                "last_asset_replacement": {
                    "schema_version": "timeline_asset_reference_replacement.v1",
                    "old_asset_id": old_asset_id,
                    "new_asset_id": new_asset_id,
                    "replacement_count": replacement_count,
                    "updated_at": replaced_at.isoformat(),
                    "updated_by": actor,
                    "comment": comment,
                },
            }
            updates.append(
                {
                    "timeline_asset_id": str(asset.id),
                    "replacement_count": replacement_count,
                    "checksum": stored.checksum,
                }
            )
        return updates

    def _replace_timeline_asset_ids(
        self,
        timeline: dict,
        old_asset_id: str,
        new_asset_id: str,
    ) -> int:
        replacement_count = 0
        segments = timeline.get("segments")
        if not isinstance(segments, list):
            return replacement_count
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            for field in self.timeline_reference_fields:
                if segment.get(field) == old_asset_id:
                    segment[field] = new_asset_id
                    replacement_count += 1
            overlay_ids = segment.get("citation_overlay_asset_ids")
            if isinstance(overlay_ids, list):
                for index, value in enumerate(overlay_ids):
                    if value == old_asset_id:
                        overlay_ids[index] = new_asset_id
                        replacement_count += 1
            visual_layers = segment.get("visual_layers")
            if isinstance(visual_layers, list):
                for layer in visual_layers:
                    if isinstance(layer, dict) and layer.get("asset_id") == old_asset_id:
                        layer["asset_id"] = new_asset_id
                        replacement_count += 1
        return replacement_count
