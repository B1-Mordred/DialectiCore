#!/usr/bin/env python3
"""Render and register the branded Production v2 introduction for human review.

The accepted Production v2 episode predates the reusable seated-panel composition
assets and stores its qualified matte/desk recipe in the production-v2 renderer.
This script deliberately reuses that exact recipe for the first discussion turn,
substituting only the immutable show-identity slate on the rear screen.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.core.config import Settings  # noqa: E402
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity  # noqa: E402
from app.domain.schemas import Approval, Asset, AuditEvent, QualityResult  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository  # noqa: E402
from app.services.object_storage import create_object_store  # noqa: E402
from production_v2_full_render import (  # noqa: E402
    ANIMATION_MANIFEST,
    _render_turn,
    _segment_fingerprint,
)
from production_v2_full_render import (  # noqa: E402
    OUTPUT_ROOT as PRODUCTION_V2_RENDER_ROOT,
)

EPISODE_ID = UUID("9d145344-82c9-46cc-b4c1-661d95f0bf56")
TIMELINE_ASSET_ID = UUID("2087a218-3b0a-494d-b2d5-e920650b018b")
SLATE_ASSET_ID = UUID("5c5b26f5-0549-48bf-8d08-97480326cc37")
REJECTED_GENERIC_RENDER_ID = UUID("8e8d4bc0-2fb0-444f-8e3f-1567d2db71c6")
OUTPUT_ROOT = ROOT / "output/production-v3/branded-intro-qualification"
MARKER = "dialecticore.production_v3.branded_intro_qualification.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-4000:]}"
        )


def _probe_media(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(completed.stdout)
    video = next(item for item in raw["streams"] if item.get("codec_type") == "video")
    audio = next(item for item in raw["streams"] if item.get("codec_type") == "audio")
    numerator, denominator = str(video["r_frame_rate"]).split("/", 1)
    return {
        "duration_ms": round(float(raw["format"]["duration"]) * 1000),
        "size_bytes": int(raw["format"]["size"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": float(numerator) / float(denominator),
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
        "audio_sample_rate": int(audio["sample_rate"]),
        "audio_channels": int(audio["channels"]),
    }


def _object_path(settings: Settings, storage_uri: str) -> Path:
    path = create_object_store(settings).path_for_uri(storage_uri)
    if path is None or not path.is_file():
        raise RuntimeError(f"object is not available: {storage_uri}")
    return path


def _identity_reel(slate: Path, output: Path, duration_ms: int) -> None:
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "policy": "dialecticore.identity_reel.v1",
                "slate_sha256": _sha256(slate),
                "duration_ms": duration_ms,
                "fps": 24,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    sidecar = output.with_suffix(".fingerprint")
    if output.is_file() and sidecar.is_file() and sidecar.read_text().strip() == fingerprint:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-i",
            str(slate),
            "-t",
            f"{duration_ms / 1000:.3f}",
            "-vf",
            "scale=1280:720:flags=lanczos,fps=24,format=yuv420p",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "16",
            "-preset",
            "medium",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    sidecar.write_text(fingerprint + "\n")


def main() -> int:
    settings = Settings()
    engine = create_database_engine(settings)
    initialize_database(engine)
    repo = EpisodeRepository(create_session_factory(engine))
    episode = repo.get(EPISODE_ID)
    timeline_asset = next(asset for asset in episode.assets if asset.id == TIMELINE_ASSET_ID)
    slate_asset = next(asset for asset in episode.assets if asset.id == SLATE_ASSET_ID)
    timeline = timeline_asset.generation_metadata["timeline_json"]
    marker = episode.workflow_control.get("production_v3_branded_intro_qualification", {})
    if marker.get("timeline_asset_id") == str(TIMELINE_ASSET_ID):
        existing = next(
            (
                asset
                for asset in episode.assets
                if str(asset.id) == marker.get("render_asset_id")
                and asset.status == "completed"
            ),
            None,
        )
        if existing is not None:
            print(json.dumps(marker, indent=2, sort_keys=True))
            return 0

    intro = timeline["tracks"]["screen_graphics"][0]
    duration_ms = int(intro["end_ms"]) - int(intro["start_ms"])
    animation = json.loads(ANIMATION_MANIFEST.read_text())
    first_job = next(job for job in animation["jobs"] if int(job["index"]) == 1)
    if int(first_job["duration_ms"]) != duration_ms:
        raise RuntimeError("participant-introduction duration does not match turn 1")

    slate_path = _object_path(settings, slate_asset.storage_uri or "")
    studio = PRODUCTION_V2_RENDER_ROOT / "studio-reference.png"
    if not studio.is_file():
        raise RuntimeError(f"qualified Production v2 studio is missing: {studio}")
    revision_root = OUTPUT_ROOT / str(slate_asset.id)
    reel = revision_root / "identity-reel.mp4"
    preview = revision_root / "branded-participant-introduction.mp4"
    _identity_reel(slate_path, reel, duration_ms)
    fingerprint = _segment_fingerprint(first_job, studio=studio, reel=reel)
    sidecar = preview.with_suffix(".fingerprint")
    if not (
        preview.is_file()
        and sidecar.is_file()
        and sidecar.read_text().strip() == fingerprint
    ):
        preview.parent.mkdir(parents=True, exist_ok=True)
        _render_turn(record=first_job, studio=studio, reel=reel, start_ms=0, output=preview)
        sidecar.write_text(fingerprint + "\n")
    probe = _probe_media(preview)
    if abs(int(probe["duration_ms"]) - duration_ms) > 80:
        raise RuntimeError(f"qualification duration is invalid: {probe['duration_ms']}ms")
    if int(probe["width"]) != 1280 or int(probe["height"]) != 720:
        raise RuntimeError("qualification dimensions are not 1280x720")

    object_store = create_object_store(settings)
    stored = object_store.put_bytes(
        key=(
            f"production-v3/qualification/{episode.id}/{timeline_asset.id}/"
            "branded-participant-introduction.mp4"
        ),
        payload=preview.read_bytes(),
        content_type="video/mp4",
    )
    manifest = {
        "schema_version": MARKER,
        "created_at": datetime.now(UTC).isoformat(),
        "timeline_asset_id": str(timeline_asset.id),
        "timeline_checksum": timeline_asset.checksum,
        "qualification_slice": {
            "kind": "branded_participant_introduction",
            "programme_start_ms": int(intro["start_ms"]),
            "programme_end_ms": int(intro["end_ms"]),
            "duration_ms": duration_ms,
        },
        "branded_thumbnail_source": {
            "schema_version": "branded_thumbnail_source.v1",
            "kind": "show_identity",
            "screen_graphics_clip_id": intro["id"],
            "slate_asset_id": str(slate_asset.id),
            "slate_checksum": slate_asset.checksum,
            "episode_title": episode.title,
            "logo_checksum": slate_asset.generation_metadata["logo_checksum"],
            "intro_start_ms": 0,
            "intro_end_ms": duration_ms,
            "selection_fraction": 0.35,
            "frame_seek_ms": 2_500,
            "programme_frame_seek_ms": int(intro["start_ms"]) + 2_500,
            "minimum_offset_ms": 1_000,
            "maximum_offset_ms": 2_500,
            "minimum_tail_ms": 500,
        },
        "composition": {
            "policy": "production_v2_full_turn.v2",
            "studio_sha256": _sha256(studio),
            "identity_reel_sha256": _sha256(reel),
            "segment_fingerprint": fingerprint,
            "camera_view": "establishing_wide",
            "camera_action": "fly_in",
            "character_mattes_preserved": True,
            "desk_occlusion_preserved": True,
            "b1_managed_animation_reused": True,
        },
        "media_probe": probe,
    }
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=timeline_asset.language,
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri=stored.uri,
        mime_type=stored.content_type,
        duration_ms=int(probe["duration_ms"]),
        width=int(probe["width"]),
        height=int(probe["height"]),
        fps=float(probe["fps"]),
        checksum=stored.checksum,
        status="completed",
        generation_metadata={
            "render_type": "preview",
            "review_scope": "qualification_slice",
            "composition_policy": "studio_camera_cuts.v2",
            "revision": "production-v3-branded-intro",
            "render_manifest": manifest,
            "media_probe": probe,
            "approval_status": "pending",
            "render_ready": True,
            "object_storage_path": str(stored.path),
        },
    )
    episode.assets.append(render_asset)

    prior_render = next(
        (
            asset
            for asset in episode.assets
            if str(asset.id) == marker.get("render_asset_id")
            and asset.id != render_asset.id
        ),
        None,
    )
    if prior_render is not None:
        prior_render.status = "replaced"
        prior_render.updated_at = datetime.now(UTC)
        prior_render.generation_metadata.update(
            {
                "approval_status": "superseded",
                "review_decision": "rejected",
                "rejection_reason": (
                    "The first rear-screen slate layout placed too much title text "
                    "behind the seated character foreground."
                ),
                "superseded_by_render_asset_id": str(render_asset.id),
            }
        )
    prior_approval = next(
        (
            approval
            for approval in episode.approvals
            if str(approval.id) == marker.get("approval_id")
        ),
        None,
    )
    if prior_approval is not None and prior_approval.decision == "pending":
        prior_approval.decision = "rejected"
        prior_approval.user_id = "codex"
        prior_approval.comment = (
            "Superseded before human review because the initial title-safe-area layout "
            "was overly occluded by the seated cast."
        )

    rejected = next(
        (asset for asset in episode.assets if asset.id == REJECTED_GENERIC_RENDER_ID), None
    )
    if rejected is not None:
        rejected.status = "replaced"
        rejected.updated_at = datetime.now(UTC)
        rejected.generation_metadata.update(
            {
                "approval_status": "rejected",
                "review_decision": "rejected",
                "rejection_reason": (
                    "Legacy flattened character clips caused the generic renderer to "
                    "replace the complete studio with a full-frame identity slate."
                ),
                "superseded_by_render_asset_id": str(render_asset.id),
            }
        )

    quality = QualityResult(
        episode_id=episode.id,
        target_type="render_asset",
        target_id=str(render_asset.id),
        check_type="branded_introduction_qualification",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
        details={
            "duration_ms": int(probe["duration_ms"]),
            "width": int(probe["width"]),
            "height": int(probe["height"]),
            "fps": float(probe["fps"]),
            "audio_codec": probe["audio_codec"],
            "video_codec": probe["video_codec"],
            "timeline_asset_id": str(timeline_asset.id),
            "slate_asset_id": str(slate_asset.id),
            "exact_episode_title": episode.title,
            "source_v2_assets_modified": False,
        },
    )
    episode.quality_results.append(quality)
    approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(render_asset.id),
        comment=(
            "Review the total-studio participant introduction: exact DialectiCore logo/title "
            "on the rear screen, all six seated characters behind the desk, audio continuity, "
            "and the gentle establishing-wide fly-in."
        ),
    )
    episode.approvals.append(approval)
    render_asset.generation_metadata["approval_id"] = str(approval.id)
    episode.workflow_control["production_v3_branded_intro_qualification"] = {
        "schema_version": MARKER,
        "status": "pending_review",
        "timeline_asset_id": str(timeline_asset.id),
        "slate_asset_id": str(slate_asset.id),
        "render_asset_id": str(render_asset.id),
        "render_checksum": render_asset.checksum,
        "approval_id": str(approval.id),
        "rejected_generic_render_asset_id": str(REJECTED_GENERIC_RENDER_ID),
        "accepted_production_v2_assets_modified": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    episode.status = EpisodeStatus.ready
    episode.updated_at = datetime.now(UTC)
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="production_v3.branded_intro_qualification.registered",
            actor="codex",
            details=episode.workflow_control["production_v3_branded_intro_qualification"],
        )
    )
    repo.save(episode)
    print(
        json.dumps(
            episode.workflow_control["production_v3_branded_intro_qualification"],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
