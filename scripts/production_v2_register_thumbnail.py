#!/usr/bin/env python3
"""Register the visually qualified Production v2 delivery thumbnail."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.domain.enums import AssetType  # noqa: E402
from app.domain.schemas import AssetReplacementRequest  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository  # noqa: E402
from app.services.asset_replacement_service import AssetReplacementService  # noqa: E402
from app.services.object_storage import create_object_store  # noqa: E402

EPISODE_ID = UUID("9d145344-82c9-46cc-b4c1-661d95f0bf56")
FINAL_RENDER_ID = UUID("7d2e95c1-56d2-4840-af59-c83c2a3c17fb")
CANDIDATE = (
    ROOT
    / "output/production-v2/full-production/thumbnail/production-v2-thumbnail.jpg"
)
MARKER = "dialecticore.production_v2.delivery_thumbnail.v1"


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=size:stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(completed.stdout)
    video = raw["streams"][0]
    return {
        "probe_tool": "ffprobe",
        "width": int(video["width"]),
        "height": int(video["height"]),
        "video_codec": video["codec_name"],
        "size_bytes": int(raw["format"]["size"]),
    }


def _validate_probe(probe: dict[str, object]) -> None:
    if probe["width"] != 1920 or probe["height"] != 1080:
        raise RuntimeError("Production v2 thumbnail must be 1920x1080")
    if probe["video_codec"] != "mjpeg":
        raise RuntimeError("Production v2 thumbnail must be a JPEG image")


def main() -> int:
    if not CANDIDATE.is_file():
        raise RuntimeError(f"Qualified thumbnail candidate is missing: {CANDIDATE}")
    probe = _probe(CANDIDATE)
    _validate_probe(probe)
    payload = CANDIDATE.read_bytes()
    checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"

    settings = Settings()
    engine = create_database_engine(settings)
    initialize_database(engine)
    repo = EpisodeRepository(create_session_factory(engine))
    episode = repo.get(EPISODE_ID)
    final_approval = next(
        (
            approval
            for approval in episode.approvals
            if approval.stage == "final_render_review"
            and approval.target_id == str(FINAL_RENDER_ID)
            and approval.decision == "approved"
        ),
        None,
    )
    if final_approval is None:
        raise RuntimeError("The Production v2 final render is not approved")

    existing = next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.thumbnail
            and asset.status == "completed"
            and asset.checksum == checksum
        ),
        None,
    )
    if existing is not None:
        print(
            json.dumps(
                {
                    "episode_id": str(episode.id),
                    "thumbnail_asset_id": str(existing.id),
                    "checksum": checksum,
                    "created": False,
                },
                indent=2,
            )
        )
        return 0

    original = next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.thumbnail and asset.status == "completed"
        ),
        None,
    )
    if original is None:
        raise RuntimeError("Generate the automatic thumbnail before replacing it")

    object_store = create_object_store(settings)
    stored = object_store.put_bytes(
        key=f"production-v2/full/{episode.id}/final/thumbnail.jpg",
        payload=payload,
        content_type="image/jpeg",
    )
    replacement_service = AssetReplacementService(settings, object_store=object_store)
    episode = replacement_service.replace_asset(
        episode,
        original.id,
        AssetReplacementRequest(
            storage_uri=stored.uri,
            mime_type="image/jpeg",
            checksum=stored.checksum,
            width=1920,
            height=1080,
            status="completed",
            generation_metadata={
                "schema_version": MARKER,
                "render_asset_id": str(FINAL_RENDER_ID),
                "render_checksum": next(
                    asset.checksum
                    for asset in episode.assets
                    if asset.id == FINAL_RENDER_ID
                ),
                "frame_seek_seconds": 359.5,
                "title_treatment": "episode_title_dark_panel.v1",
                "visual_review": "passed",
                "media_probe": probe,
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
            },
            user_id="codex",
            comment=(
                "Replace the automatic mid-program crop with the visually reviewed "
                "closing total-studio frame and restrained episode-title treatment."
            ),
        ),
    )
    replacement = episode.assets[-1]
    episode.workflow_control["production_v2_thumbnail"] = {
        "schema_version": MARKER,
        "thumbnail_asset_id": str(replacement.id),
        "render_asset_id": str(FINAL_RENDER_ID),
        "checksum": replacement.checksum,
        "status": "approved_for_packaging",
    }
    repo.save(episode)
    print(
        json.dumps(
            {
                "episode_id": str(episode.id),
                "thumbnail_asset_id": str(replacement.id),
                "replaces_asset_id": str(original.id),
                "checksum": replacement.checksum,
                "created": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
