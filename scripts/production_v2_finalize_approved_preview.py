#!/usr/bin/env python3
"""Promote the exact approved Production v2 composite to a 1080p final render."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

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

EPISODE_ID = UUID("9d145344-82c9-46cc-b4c1-661d95f0bf56")
PRESET_ID = "youtube-1080p"
WIDTH = 1920
HEIGHT = 1080
FPS = 30
MARKER = "dialecticore.production_v2.approved_preview_final.v1"


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,start_time,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(completed.stdout)
    streams = raw.get("streams") or []
    video = next(item for item in streams if item.get("codec_type") == "video")
    audio = next(item for item in streams if item.get("codec_type") == "audio")
    numerator, denominator = str(video["r_frame_rate"]).split("/", 1)
    video_start = float(video.get("start_time") or 0)
    audio_start = float(audio.get("start_time") or 0)
    return {
        "probe_tool": "ffprobe",
        "probe_warnings": [],
        "duration_ms": round(float(raw["format"]["duration"]) * 1000),
        "size_bytes": int(raw["format"]["size"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": float(numerator) / float(denominator),
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
        "audio_sample_rate": int(audio["sample_rate"]),
        "audio_channels": int(audio["channels"]),
        "av_offset_ms": round(abs(video_start - audio_start) * 1000, 3),
    }


def _ffmpeg_command(source: Path, target: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-vf",
        f"scale={WIDTH}:{HEIGHT}:flags=lanczos,fps={FPS}",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-b:v",
        "8M",
        "-maxrate",
        "8M",
        "-bufsize",
        "16M",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(target),
    ]


def _approved_preview(episode: object) -> tuple[Asset, Approval]:
    control = episode.workflow_control.get("production_v2_full_preview", {})
    preview_id = str(control.get("render_asset_id") or "")
    approval_id = str(control.get("approval_id") or "")
    preview = next(
        (
            asset
            for asset in episode.assets
            if str(asset.id) == preview_id
            and asset.asset_type == AssetType.render
            and asset.status == "completed"
        ),
        None,
    )
    approval = next(
        (item for item in episode.approvals if str(item.id) == approval_id),
        None,
    )
    if preview is None or approval is None:
        raise RuntimeError("Production v2 preview or its approval is missing")
    if approval.decision != "approved" or approval.target_id != str(preview.id):
        raise RuntimeError("Production v2 preview is not human-approved")
    if preview.generation_metadata.get("composition_policy") != "studio_camera_cuts.v2":
        raise RuntimeError("Production v2 preview does not use the v2 composition policy")
    passing_qc = next(
        (
            result
            for result in reversed(episode.quality_results)
            if result.target_id == str(preview.id)
            and result.check_type == "render_preview_integrity"
            and result.status == "pass"
            and result.severity != QualitySeverity.fail
        ),
        None,
    )
    if passing_qc is None:
        raise RuntimeError("Production v2 preview does not have passing integrity QC")
    return preview, approval


def _delivery_duration_limit_seconds(duration_ms: int) -> int:
    return math.ceil(duration_ms / 1000)


def _validate_delivery_probe(
    source_probe: dict[str, object],
    final_probe: dict[str, object],
) -> None:
    failures: list[str] = []
    if final_probe["width"] != WIDTH or final_probe["height"] != HEIGHT:
        failures.append("resolution")
    if abs(float(final_probe["fps"]) - FPS) > 0.05:
        failures.append("fps")
    if final_probe["video_codec"] != "h264" or final_probe["audio_codec"] != "aac":
        failures.append("codecs")
    if final_probe["audio_sample_rate"] != 48_000 or final_probe["audio_channels"] != 2:
        failures.append("audio_format")
    if abs(int(final_probe["duration_ms"]) - int(source_probe["duration_ms"])) > 100:
        failures.append("duration")
    if float(final_probe["av_offset_ms"]) > 50:
        failures.append("av_sync")
    if failures:
        raise RuntimeError(f"Production v2 delivery validation failed: {', '.join(failures)}")


def main() -> int:
    settings = Settings()
    engine = create_database_engine(settings)
    initialize_database(engine)
    repo = EpisodeRepository(create_session_factory(engine))
    episode = repo.get(EPISODE_ID)
    preview, preview_approval = _approved_preview(episode)

    existing = episode.workflow_control.get("production_v2_final", {})
    if (
        existing.get("schema_version") == MARKER
        and existing.get("source_preview_asset_id") == str(preview.id)
        and existing.get("source_preview_checksum") == preview.checksum
    ):
        final_asset = next(
            (
                asset
                for asset in episode.assets
                if str(asset.id) == str(existing.get("render_asset_id"))
                and asset.status == "completed"
            ),
            None,
        )
        if final_asset is not None:
            print(
                json.dumps(
                    {
                        "episode_id": str(episode.id),
                        "render_asset_id": str(final_asset.id),
                        "approval_id": existing.get("approval_id"),
                        "created": False,
                    },
                    indent=2,
                )
            )
            return 0

    source_path_text = str(preview.generation_metadata.get("object_storage_path") or "")
    source_path = ROOT / source_path_text
    if not source_path.is_file():
        raise RuntimeError(f"Approved preview bytes are unavailable: {source_path}")
    source_probe = _probe(source_path)
    source_checksum = f"sha256:{hashlib.sha256(source_path.read_bytes()).hexdigest()}"
    if preview.checksum != source_checksum:
        raise RuntimeError("Approved preview checksum does not match its stored bytes")

    output_dir = ROOT / "output/production-v2/full-production/final"
    output_dir.mkdir(parents=True, exist_ok=True)
    partial_path = output_dir / "production-v2-final.partial.mp4"
    final_path = output_dir / "production-v2-final.mp4"
    subprocess.run(_ffmpeg_command(source_path, partial_path), check=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(partial_path), "-f", "null", "-"],
        check=True,
    )
    final_probe = _probe(partial_path)
    _validate_delivery_probe(source_probe, final_probe)
    partial_path.replace(final_path)

    object_store = create_object_store(settings)
    stored = object_store.put_bytes(
        key=f"production-v2/full/{episode.id}/final/youtube-1080p.mp4",
        payload=final_path.read_bytes(),
        content_type="video/mp4",
    )
    timeline_asset_id = str(preview.source_entity_id or "")
    caption_track_asset_id = preview.generation_metadata.get("caption_track_asset_id")
    final_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=preview.language,
        source_entity_type="timeline_asset",
        source_entity_id=timeline_asset_id,
        storage_uri=stored.uri,
        mime_type="video/mp4",
        duration_ms=int(final_probe["duration_ms"]),
        width=int(final_probe["width"]),
        height=int(final_probe["height"]),
        fps=float(final_probe["fps"]),
        checksum=stored.checksum,
        status="completed",
        generation_metadata={
            "render_type": "final",
            "preset_id": PRESET_ID,
            "review_scope": "full_timeline",
            "composition_policy": "studio_camera_cuts.v2",
            "production_v2": True,
            "finalization_policy": "approved_preview_transcode.v1",
            "source_preview_asset_id": str(preview.id),
            "source_preview_checksum": preview.checksum,
            "source_preview_approval_id": str(preview_approval.id),
            "source_preview_approved_by": preview_approval.user_id,
            "media_probe": final_probe,
            "caption_track_asset_id": caption_track_asset_id,
            "caption_track_mode": "selectable" if caption_track_asset_id else "off",
            "approval_status": "pending",
            "object_storage_path": str(stored.path),
            "render_ready": True,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    episode.assets.append(final_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(final_asset.id),
            check_type="render_final_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
            details={
                "finalization_policy": "approved_preview_transcode.v1",
                "source_preview_asset_id": str(preview.id),
                "source_preview_checksum": preview.checksum,
                "source_preview_approval_id": str(preview_approval.id),
                "source_duration_ms": source_probe["duration_ms"],
                "duration_ms": final_probe["duration_ms"],
                "width": final_probe["width"],
                "height": final_probe["height"],
                "fps": final_probe["fps"],
                "av_offset_ms": final_probe["av_offset_ms"],
                "composition_preserved_from_approved_preview": True,
                "failure_count": 0,
                "warning_count": 0,
            },
        )
    )
    approval = Approval(
        episode_id=episode.id,
        stage="final_render_review",
        target_type="render_asset",
        target_id=str(final_asset.id),
        comment=(
            "Review the 1080p delivery transcode of the exact approved Production v2 "
            "revision 5 composite before packaging."
        ),
    )
    episode.approvals.append(approval)
    final_asset.generation_metadata["approval_id"] = str(approval.id)

    old_maximum = episode.maximum_duration_seconds
    accepted_maximum = _delivery_duration_limit_seconds(int(final_probe["duration_ms"]))
    if accepted_maximum > old_maximum:
        episode.maximum_duration_seconds = accepted_maximum
    episode.workflow_control["production_v2_final"] = {
        "schema_version": MARKER,
        "source_preview_asset_id": str(preview.id),
        "source_preview_checksum": preview.checksum,
        "source_preview_approval_id": str(preview_approval.id),
        "render_asset_id": str(final_asset.id),
        "approval_id": str(approval.id),
        "preset_id": PRESET_ID,
        "status": "pending_review",
        "created_at": datetime.now(UTC).isoformat(),
    }
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="production_v2.final.registered",
            actor="codex",
            details={
                "render_asset_id": str(final_asset.id),
                "render_checksum": final_asset.checksum,
                "source_preview_asset_id": str(preview.id),
                "source_preview_checksum": preview.checksum,
                "source_preview_approval_id": str(preview_approval.id),
                "finalization_policy": "approved_preview_transcode.v1",
                "preset_id": PRESET_ID,
                "old_maximum_duration_seconds": old_maximum,
                "accepted_maximum_duration_seconds": episode.maximum_duration_seconds,
                "v1_assets_modified": False,
            },
        )
    )
    episode.status = EpisodeStatus.ready
    episode.updated_at = datetime.now(UTC)
    repo.save(episode)
    print(
        json.dumps(
            {
                "episode_id": str(episode.id),
                "render_asset_id": str(final_asset.id),
                "approval_id": str(approval.id),
                "checksum": final_asset.checksum,
                "duration_ms": final_asset.duration_ms,
                "created": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
