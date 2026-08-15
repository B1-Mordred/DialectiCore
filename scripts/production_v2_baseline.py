#!/usr/bin/env python3
"""Capture reproducible v1 character-media evidence before v2 generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECT_ROOT = REPO_ROOT / "storage/object-store/dialecticore"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_path(uri: str | None) -> Path | None:
    prefix = "object://dialecticore/"
    if not isinstance(uri, str) or not uri.startswith(prefix):
        return None
    return OBJECT_ROOT / uri.removeprefix(prefix)


def probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,bit_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def video_metric(path: Path, filter_name: str, pattern: str, seconds: int = 3) -> float | None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-vf",
            filter_name,
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(pattern, completed.stderr)
    return round(float(match.group(1)), 6) if match else None


def media_record(uri: str | None, *, metrics: bool = False) -> dict[str, Any] | None:
    path = object_path(uri)
    if path is None or not path.is_file():
        return None
    result: dict[str, Any] = {
        "uri": uri,
        "relative_path": str(path.relative_to(REPO_ROOT)),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "probe": probe(path),
    }
    if metrics:
        result["block_mean_first_3s"] = video_metric(
            path, "blockdetect", r"block mean:\s*([0-9.]+)"
        )
        result["blur_mean_first_3s"] = video_metric(path, "blurdetect", r"blur mean:\s*([0-9.]+)")
    return result


def first_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def reference_uri(asset: dict[str, Any], kind: str) -> str | None:
    metadata = first_dict(asset.get("generation_metadata"))
    inputs = first_dict(metadata.get("prompt_inputs"))
    direct = inputs.get(f"{kind}_reference_image_uri")
    if isinstance(direct, str):
        return direct
    references = first_dict(inputs.get("reference_images"))
    reference = first_dict(references.get(kind))
    uri = reference.get("uri")
    return uri if isinstance(uri, str) else None


def input_upload_bytes(asset: dict[str, Any], field: str) -> int | None:
    metadata = first_dict(asset.get("generation_metadata"))
    response = first_dict(metadata.get("provider_response"))
    request = first_dict(response.get("redacted_request"))
    inputs = first_dict(request.get("input"))
    value = first_dict(inputs.get(f"{field}_artifact_id"))
    size = value.get("bytes")
    return int(size) if isinstance(size, int) else None


def region_pixels(asset: dict[str, Any], region_name: str) -> dict[str, int] | None:
    metadata = first_dict(asset.get("generation_metadata"))
    seated = first_dict(metadata.get("seated_character"))
    region = first_dict(seated.get(region_name))
    width = asset.get("width")
    height = asset.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or not region:
        return None
    return {
        "x": round(float(region.get("x", 0)) * width),
        "y": round(float(region.get("y", 0)) * height),
        "width": round(float(region.get("width", 0)) * width),
        "height": round(float(region.get("height", 0)) * height),
    }


def active_assets(
    assets: list[dict[str, Any]], *, role: str, asset_type: str
) -> list[dict[str, Any]]:
    return [
        asset
        for asset in assets
        if asset.get("status") == "completed"
        and asset.get("asset_type") == asset_type
        and first_dict(asset.get("generation_metadata")).get("visual_role") == role
    ]


def capture(api_base: str, episode_id: str) -> dict[str, Any]:
    with urllib.request.urlopen(
        f"{api_base.rstrip('/')}/api/v1/episodes/{episode_id}", timeout=30
    ) as response:
        episode = json.load(response)
    assets = episode.get("assets") if isinstance(episode.get("assets"), list) else []
    transcripts = episode.get("transcripts") if isinstance(episode.get("transcripts"), list) else []
    canonical = next(
        (
            transcript
            for transcript in transcripts
            if transcript.get("id") == episode.get("canonical_transcript_version_id")
        ),
        None,
    )
    if canonical is None:
        canonical = next(
            (item for item in reversed(transcripts) if item.get("status") == "approved"), {}
        )
    turns = canonical.get("turns") if isinstance(canonical, dict) else []
    speaker_by_turn = {
        str(turn.get("id")): str(turn.get("speaker_participant_id"))
        for turn in turns or []
        if turn.get("id") and turn.get("speaker_participant_id")
    }
    seated_assets = active_assets(assets, role="studio_seated_character", asset_type="image")
    videos = active_assets(assets, role="video_primary", asset_type="video")
    speakers = sorted(
        {*speaker_by_turn.values(), *(str(item.get("source_entity_id")) for item in seated_assets)}
    )
    character_rows: list[dict[str, Any]] = []
    for speaker in speakers:
        seated = next(
            (item for item in reversed(seated_assets) if item.get("source_entity_id") == speaker),
            None,
        )
        speaking = [
            item
            for item in videos
            if speaker_by_turn.get(str(item.get("source_entity_id"))) == speaker
        ]
        representative = speaking[0] if speaking else None
        metadata = first_dict(seated.get("generation_metadata")) if seated else {}
        portrait = media_record(reference_uri(seated, "portrait") if seated else None)
        full_body = media_record(reference_uri(seated, "full_body") if seated else None)
        character_rows.append(
            {
                "participant_id": speaker,
                "portrait_reference": portrait,
                "full_body_reference": full_body,
                "seated_asset_id": seated.get("id") if seated else None,
                "seated_plate": media_record(seated.get("storage_uri") if seated else None),
                "seated_body_region_px": region_pixels(seated, "body_region") if seated else None,
                "seated_face_region_px": region_pixels(seated, "face_region") if seated else None,
                "transport": {
                    "portrait_repacked": first_dict(
                        first_dict(metadata.get("b1_upload_references")).get(speaker)
                    ).get("portrait_repacked_for_b1"),
                    "full_body_repacked": first_dict(
                        first_dict(metadata.get("b1_upload_references")).get(speaker)
                    ).get("full_body_repacked_for_b1"),
                    "provider_portrait_bytes": input_upload_bytes(seated, "portrait")
                    if seated
                    else None,
                    "provider_full_body_bytes": input_upload_bytes(seated, "full_body")
                    if seated
                    else None,
                },
                "speaking_clip_count": len(speaking),
                "representative_speaking_asset_id": representative.get("id")
                if representative
                else None,
                "representative_speaking_clip": media_record(
                    representative.get("storage_uri") if representative else None,
                    metrics=True,
                ),
            }
        )
    final_asset = next(
        (
            asset
            for asset in reversed(assets)
            if asset.get("id") == "37fa74da-820c-49c7-96c2-d973dc6efb46"
        ),
        None,
    )
    return {
        "schema_version": "dialecticore.production_v2_baseline.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "episode_id": episode_id,
        "episode_title": first_dict(episode.get("definition")).get("title"),
        "canonical_transcript_version_id": canonical.get("id")
        if isinstance(canonical, dict)
        else None,
        "characters": character_rows,
        "final_render_asset_id": final_asset.get("id") if final_asset else None,
        "final_render": media_record(final_asset.get("storage_uri") if final_asset else None),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# DialectiCore production v2 baseline",
        "",
        f"Captured: `{report['captured_at']}`",
        "",
        "| Character | Source portrait | Source body | Seated face | Seated body | "
        "B1 portrait | B1 body | Speaking | Block | Blur |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["characters"]:
        portrait = row.get("portrait_reference") or {}
        body = row.get("full_body_reference") or {}
        face = row.get("seated_face_region_px") or {}
        seated_body = row.get("seated_body_region_px") or {}
        clip = row.get("representative_speaking_clip") or {}
        transport = row.get("transport") or {}
        lines.append(
            "| {participant} | {portrait_bytes} | {body_bytes} | {face_w}x{face_h} | "
            "{body_w}x{body_h} | {upload_portrait} | {upload_body} | {count} | "
            "{block} | {blur} |".format(
                participant=row["participant_id"],
                portrait_bytes=portrait.get("bytes", "-"),
                body_bytes=body.get("bytes", "-"),
                face_w=face.get("width", "-"),
                face_h=face.get("height", "-"),
                body_w=seated_body.get("width", "-"),
                body_h=seated_body.get("height", "-"),
                upload_portrait=transport.get("provider_portrait_bytes") or "-",
                upload_body=transport.get("provider_full_body_bytes") or "-",
                count=row.get("speaking_clip_count", 0),
                block=clip.get("block_mean_first_3s", "-"),
                blur=clip.get("blur_mean_first_3s", "-"),
            )
        )
    lines.extend(
        [
            "",
            "The JSON companion contains exact paths, probes, sizes, checksums, "
            "transport flags, and representative clip metrics.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--episode-id", default="cc1ad449-9cad-4a40-a150-652db0b7dc7a")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "output/production-v2")
    args = parser.parse_args()
    report = capture(args.api_base, args.episode_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "v1-character-quality-baseline.json"
    md_path = args.output_dir / "v1-character-quality-baseline.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
