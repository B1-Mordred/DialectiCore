#!/usr/bin/env python3
"""Render a bounded UI-camera proof from the active immutable timeline version."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.domain.defaults import default_render_presets  # noqa: E402
from app.domain.schemas import RenderRequest  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository  # noqa: E402
from app.services.render_service import RenderService  # noqa: E402

EPISODE_ID = UUID("9d145344-82c9-46cc-b4c1-661d95f0bf56")
PROOF_START_MS = 63_927
PROOF_END_MS = 125_555
OUTPUT_ROOT = ROOT / "output/production-v3/editorial-camera-proof"


def _slice(timeline: dict, start_ms: int, end_ms: int) -> dict:
    segments: list[dict] = []
    for segment in timeline.get("segments", []):
        if not isinstance(segment, dict):
            continue
        source_start = int(segment.get("start_ms") or 0)
        source_end = int(segment.get("end_ms") or 0)
        clipped_start = max(source_start, start_ms)
        clipped_end = min(source_end, end_ms)
        if clipped_end <= clipped_start:
            continue
        delta = clipped_start - source_start
        piece = {
            **segment,
            "start_ms": clipped_start - start_ms,
            "end_ms": clipped_end - start_ms,
            "duration_ms": clipped_end - clipped_start,
            "audio_source_offset_ms": int(segment.get("audio_source_offset_ms") or 0)
            + delta,
        }
        if segment.get("source_start_ms") is not None:
            piece["source_start_ms"] = int(segment.get("source_start_ms") or 0) + delta
            piece["source_end_ms"] = piece["source_start_ms"] + piece["duration_ms"]
        segments.append(piece)

    tracks: dict[str, object] = {}
    for track_name, raw_clips in timeline.get("tracks", {}).items():
        if not isinstance(raw_clips, list):
            tracks[track_name] = raw_clips
            continue
        clips: list[dict] = []
        for clip in raw_clips:
            if not isinstance(clip, dict):
                continue
            clip_start = int(clip.get("start_ms") or 0)
            clip_end = int(clip.get("end_ms") or 0)
            clipped_start = max(clip_start, start_ms)
            clipped_end = min(clip_end, end_ms)
            if clipped_end <= clipped_start:
                continue
            delta = clipped_start - clip_start
            rebased = {
                **clip,
                "start_ms": clipped_start - start_ms,
                "end_ms": clipped_end - start_ms,
                "duration_ms": clipped_end - clipped_start,
            }
            if clip.get("source_in_ms") is not None:
                rebased["source_in_ms"] = int(clip.get("source_in_ms") or 0) + delta
                rebased["source_out_ms"] = (
                    rebased["source_in_ms"] + clipped_end - clipped_start
                )
            clips.append(rebased)
        tracks[track_name] = clips
    return {
        **timeline,
        "duration_ms": end_ms - start_ms,
        "segments": segments,
        "tracks": tracks,
        "review_scope": "editorial_camera_proof",
    }


def main() -> int:
    settings = Settings()
    engine = create_database_engine(settings)
    initialize_database(engine)
    episode = EpisodeRepository(create_session_factory(engine)).get(EPISODE_ID)
    service = RenderService(settings)
    timeline_asset = service._target_timeline_asset(episode, RenderRequest())
    timeline = service._materialized_timeline_for_render(
        episode,
        service._timeline_json(timeline_asset),
    )
    proof_timeline = _slice(
        service._timeline_render_view(timeline),
        PROOF_START_MS,
        PROOF_END_MS,
    )
    service._ensure_studio_camera_renderable(episode, proof_timeline)
    preset = next(
        item for item in default_render_presets() if item.id == "preview-low-bitrate"
    ).model_copy(update={"video_bitrate": "8M", "audio_bitrate": "192k"})
    proof_id = str(uuid4())
    media = service._render_media_bytes(
        episode=episode,
        timeline=proof_timeline,
        preset=preset,
        manifest={"id": proof_id},
        render_type="preview",
    )
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_ROOT / f"timeline-{timeline_asset.id}.mp4"
    output.write_bytes(media)
    directions = [
        {
            "id": clip.get("id"),
            "view": clip.get("view"),
            "action": clip.get("action"),
            "from_participant_id": clip.get("from_participant_id"),
            "target_participant_id": clip.get("target_participant_id"),
            "easing": clip.get("easing"),
        }
        for clip in proof_timeline.get("tracks", {}).get("camera_direction", [])
    ]
    result = {
        "schema_version": "dialecticore.editorial_camera_proof.v1",
        "episode_id": str(episode.id),
        "timeline_asset_id": str(timeline_asset.id),
        "timeline_checksum": timeline_asset.checksum,
        "programme_start_ms": PROOF_START_MS,
        "programme_end_ms": PROOF_END_MS,
        "duration_ms": PROOF_END_MS - PROOF_START_MS,
        "camera_direction": directions,
        "broll_presentation": proof_timeline.get("tracks", {}).get(
            "broll_presentation", []
        ),
        "output": str(output.relative_to(ROOT)),
        "sha256": hashlib.sha256(media).hexdigest(),
    }
    (OUTPUT_ROOT / f"timeline-{timeline_asset.id}.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
