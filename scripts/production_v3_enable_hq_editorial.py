#!/usr/bin/env python3
"""Attach the qualified Production-v2 studio recipe to the active editable timeline.

The operation publishes immutable copies of the exact alpha masters used by the
qualified renderer, then saves the contract through the normal timeline API so
the edit remains versioned and auditable.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.core.config import Settings  # noqa: E402
from app.services.object_storage import create_object_store  # noqa: E402
from production_v2_integrated_qualification import (  # noqa: E402
    CHARACTER_CANVAS_SIZE,
    DESK_OCCLUSION_OVERLAP,
    DESK_TOP,
    MATTE_GEOMETRY,
    PARTICIPANTS,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SCREEN_X,
    SCREEN_Y,
    STUDIO_URI,
    _master_path,
)

EPISODE_ID = UUID("9d145344-82c9-46cc-b4c1-661d95f0bf56")
API_ROOT = "http://127.0.0.1:8000/api/v1"
FULL_SEAT_CENTERS_X = (440, 597, 754, 911, 1068, 1225)


def _request(url: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        return json.load(response)


def main() -> int:
    response = _request(f"{API_ROOT}/episodes/{EPISODE_ID}/timeline")
    timeline = response["timeline"]
    object_store = create_object_store(Settings())
    matte_uris: dict[str, str] = {}
    matte_checksums: dict[str, str] = {}
    for participant_id in PARTICIPANTS:
        source = _master_path(participant_id)
        if not source.is_file():
            raise RuntimeError(f"qualified seated master is missing: {source}")
        stored = object_store.put_bytes(
            key=(
                f"production-v3/editorial/{EPISODE_ID}/seated-masters/"
                f"{participant_id}.png"
            ),
            payload=source.read_bytes(),
            content_type="image/png",
        )
        matte_uris[participant_id] = stored.uri
        matte_checksums[participant_id] = stored.checksum

    render_contract = timeline.setdefault("render_contract", {})
    render_contract["high_quality_studio"] = {
        "policy": "high_quality_seated_performance.v1",
        "studio_reference_uri": STUDIO_URI,
        "matte_uris": matte_uris,
        "matte_checksums": matte_checksums,
        "participant_order": list(PARTICIPANTS),
        "seat_centers_x": list(FULL_SEAT_CENTERS_X),
        "canvas_width": 1672,
        "canvas_height": 941,
        "camera_top": 190,
        "screen": {
            "x": SCREEN_X,
            "y": SCREEN_Y,
            "width": SCREEN_WIDTH,
            "height": SCREEN_HEIGHT,
        },
        "desk_top": DESK_TOP,
        "desk_overlap": DESK_OCCLUSION_OVERLAP,
        "edge_desk_contact_extra": 18,
        "wide_character_scale": 0.82,
        "character_canvas_size": CHARACTER_CANVAS_SIZE,
        "matte_geometry": MATTE_GEOMETRY,
        "performance_role": "production_v2_speaking_character",
        "camera_policy": "ui_direction_over_composited_performance.v1",
        "intermediate_video_crf": 16,
    }
    for clip in timeline.get("tracks", {}).get("broll_presentation", []):
        clip.setdefault("fit", "contain")
        clip.setdefault("focal_y", 0.5)

    updated = _request(
        f"{API_ROOT}/episodes/{EPISODE_ID}/timeline",
        method="PUT",
        payload={
            "timeline": timeline,
            "user_id": "codex",
            "comment": (
                "Enable qualified high-resolution seated-performance composition while "
                "preserving UI camera and B-roll editorial control."
            ),
        },
    )
    latest = _request(f"{API_ROOT}/episodes/{EPISODE_ID}/timeline")
    print(
        json.dumps(
            {
                "episode_id": str(EPISODE_ID),
                "timeline_asset_id": latest["asset"]["id"],
                "edit_version": latest["timeline"]["edit_version"],
                "matte_uris": matte_uris,
                "asset_count": len(updated.get("assets", [])),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
