#!/usr/bin/env python3
"""Register immutable production-v2 qualification evidence in a new episode."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.domain.enums import AssetType, EpisodeStatus  # noqa: E402
from app.domain.schemas import (  # noqa: E402
    Approval,
    Asset,
    AuditEvent,
    EpisodeCreateRequest,
)
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository  # noqa: E402
from app.services.object_storage import create_object_store  # noqa: E402

SOURCE_EPISODE_ID = UUID("cc1ad449-9cad-4a40-a150-652db0b7dc7a")
QUALIFICATION_ROOT = ROOT / "output/production-v2/integrated-qualification"
MASTER_MANIFEST_PATH = ROOT / "output/production-v2/normalized-seated-masters/manifest.json"
ANIMATION_ANALYSIS_PATH = ROOT / "output/production-v2/animation-qualification/analysis.json"


def main() -> int:
    settings = Settings()
    engine = create_database_engine(settings)
    initialize_database(engine)
    repo = EpisodeRepository(create_session_factory(engine))
    source = repo.get(SOURCE_EPISODE_ID)
    marker = "dialecticore.production_v2.qualification.v1"
    qualification_title = f"{source.definition.title} — Production v2 qualification"
    episodes = list(repo.list_compact())
    existing = next(
        (
            episode
            for episode in episodes
            if episode.workflow_control.get("production_v2_qualification", {}).get("schema_version")
            == marker
            and episode.workflow_control["production_v2_qualification"].get("source_episode_id")
            == str(SOURCE_EPISODE_ID)
        ),
        None,
    )
    if existing is not None:
        print(json.dumps({"episode_id": str(existing.id), "created": False}, indent=2))
        return 0

    incomplete = next(
        (
            episode
            for episode in episodes
            if episode.title == qualification_title
            and "production_v2_qualification" not in episode.workflow_control
            and not episode.assets
        ),
        None,
    )
    if incomplete is not None:
        episode = repo.get(incomplete.id)
    else:
        definition = source.definition.model_copy(deep=True)
        definition.title = qualification_title
        episode = repo.create(
            EpisodeCreateRequest(
                project_id=source.project_id,
                definition=definition,
                participants=source.participants,
                model_endpoints=source.model_endpoints,
            )
        )
    object_store = create_object_store(settings)
    master_manifest = json.loads(MASTER_MANIFEST_PATH.read_text())
    analysis = json.loads(ANIMATION_ANALYSIS_PATH.read_text())
    selection = analysis["selection"]

    master_asset_ids: list[str] = []
    for record in master_manifest["records"]:
        participant_id = record["participant_id"]
        selected_candidate = selection[participant_id]
        source_path = ROOT / record["master"]["path"]
        stored = object_store.put_bytes(
            key=(f"production-v2/qualification/{episode.id}/seated-masters/{participant_id}.png"),
            payload=source_path.read_bytes(),
            content_type="image/png",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.image,
            source_entity_type="participant_profile",
            source_entity_id=participant_id,
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            width=record["geometry"]["canvas"]["width"],
            height=record["geometry"]["canvas"]["height"],
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "schema_version": record["schema_version"],
                "visual_role": "studio_seated_character_v2_master",
                "normalization_version": record["normalization_version"],
                "geometry": record["geometry"],
                "source": record["source"],
                "enhancement": record["enhancement"],
                "qc": record["qc"],
                "v1_asset_id": record["v1_asset_id"],
                "animation_input_policy": {
                    "selected_candidate": selected_candidate,
                    "reason": (
                        "native_scale_face_detector_compatibility"
                        if participant_id == "deepseek"
                        else "enhanced_normalized_master"
                    ),
                },
                "object_storage_path": str(stored.path),
                "render_ready": True,
                "immutable_qualification_asset": True,
            },
        )
        episode.assets.append(asset)
        master_asset_ids.append(str(asset.id))

    render_path = QUALIFICATION_ROOT / "production-v2-integrated-qualification.mp4"
    render_manifest = json.loads((QUALIFICATION_ROOT / "manifest.json").read_text())
    stored_render = object_store.put_bytes(
        key=f"production-v2/qualification/{episode.id}/integrated-qualification.mp4",
        payload=render_path.read_bytes(),
        content_type="video/mp4",
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        source_entity_type="production_v2_qualification",
        source_entity_id=str(SOURCE_EPISODE_ID),
        storage_uri=stored_render.uri,
        mime_type=stored_render.content_type,
        duration_ms=24_063,
        width=1280,
        height=720,
        fps=24,
        checksum=stored_render.checksum,
        status="completed",
        generation_metadata={
            "schema_version": render_manifest["schema_version"],
            "visual_role": "production_v2_integrated_qualification",
            "render_type": "qualification",
            "render_manifest": render_manifest,
            "master_asset_ids": master_asset_ids,
            "animation_analysis": analysis,
            "object_storage_path": str(stored_render.path),
            "render_ready": True,
            "approval_status": "pending",
            "immutable_qualification_asset": True,
        },
    )
    episode.assets.append(render_asset)
    approval = Approval(
        episode_id=episode.id,
        stage="production_v2_integrated_qualification_review",
        target_type="render_asset",
        target_id=str(render_asset.id),
        comment=(
            "Review all six normalized speakers, lip motion, speaker-centered framing, "
            "rear-screen B-roll, and the fullscreen round trip before full v2 production."
        ),
    )
    episode.approvals.append(approval)
    episode.workflow_control["production_v2_qualification"] = {
        "schema_version": marker,
        "source_episode_id": str(SOURCE_EPISODE_ID),
        "render_asset_id": str(render_asset.id),
        "master_asset_ids": master_asset_ids,
        "approval_id": str(approval.id),
        "status": "pending_review",
        "created_at": datetime.now(UTC).isoformat(),
    }
    episode.status = EpisodeStatus.ready
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="production_v2.qualification.registered",
            actor="codex",
            details={
                "source_episode_id": str(SOURCE_EPISODE_ID),
                "render_asset_id": str(render_asset.id),
                "render_checksum": render_asset.checksum,
                "master_asset_ids": master_asset_ids,
                "v1_assets_modified": False,
            },
        )
    )
    episode.updated_at = datetime.now(UTC)
    repo.save(episode)
    print(
        json.dumps(
            {
                "episode_id": str(episode.id),
                "render_asset_id": str(render_asset.id),
                "approval_id": str(approval.id),
                "created": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
