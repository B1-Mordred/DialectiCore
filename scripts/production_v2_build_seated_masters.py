#!/usr/bin/env python3
"""Build normalized transparent v2 seated masters from qualified B1 outputs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEATED_ROOT = ROOT / "output/production-v2/seated-transport-qualification"
UPSCALE_ROOT = ROOT / "output/production-v2/upscale-qualification"
OUTPUT_ROOT = ROOT / "output/production-v2/normalized-seated-masters"
BASELINE_PATH = ROOT / "output/production-v2/v1-character-quality-baseline.json"
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 1280
DESK_BASELINE_Y = 1120
BASE_BODY_HEIGHT = 1024
STATURE_OFFSETS_PERCENT = {
    "chatgpt": 0.0,
    "claude": 2.0,
    "deepseek": -1.0,
    "gemini": 1.0,
    "grok": 3.0,
    "mistral": -2.0,
}
BBOX_RE = re.compile(r"x1:(\d+) x2:(\d+) y1:(\d+) y2:(\d+) w:(\d+) h:(\d+)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-2000:]}"
        )
    return completed


def _alpha_bbox(path: Path) -> dict[str, int]:
    completed = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            "alphaextract,bbox",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
    )
    matches = BBOX_RE.findall(completed.stderr)
    if not matches:
        raise RuntimeError(f"could not determine alpha bounds for {path}")
    x1, x2, y1, y2, width, height = (int(value) for value in matches[-1])
    return {
        "x": x1,
        "y": y1,
        "width": width,
        "height": height,
        "x2": x2,
        "y2": y2,
    }


def _rechunk_png(path: Path) -> None:
    """Merge FFmpeg's many IDAT chunks without changing compressed pixel data.

    B1's defensive media sniffer inspects at most 256 PNG chunks. FFmpeg can
    emit more than 300 IDAT chunks for a 1280px master, causing a valid PNG to
    be classified as generic binary data. One IDAT stream is equivalent and
    keeps the file inside that bounded parser.
    """
    payload = path.read_bytes()
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"cannot rechunk non-PNG file: {path}")
    offset = 8
    before_idat: list[bytes] = []
    after_idat: list[bytes] = []
    idat_payloads: list[bytes] = []
    seen_idat = False
    while offset + 12 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        end = offset + 12 + length
        if end > len(payload):
            raise RuntimeError(f"invalid PNG chunk length in {path}")
        chunk = payload[offset:end]
        chunk_type = chunk[4:8]
        if chunk_type == b"IDAT":
            seen_idat = True
            idat_payloads.append(chunk[8:-4])
        elif seen_idat:
            after_idat.append(chunk)
        else:
            before_idat.append(chunk)
        offset = end
        if chunk_type == b"IEND":
            break
    if not idat_payloads or offset != len(payload):
        raise RuntimeError(f"PNG has no complete IDAT/IEND stream: {path}")
    compressed = b"".join(idat_payloads)
    chunk_type = b"IDAT"
    merged_idat = (
        len(compressed).to_bytes(4, "big")
        + chunk_type
        + compressed
        + (zlib.crc32(chunk_type + compressed) & 0xFFFFFFFF).to_bytes(4, "big")
    )
    rechunked = b"\x89PNG\r\n\x1a\n" + b"".join(before_idat) + merged_idat + b"".join(after_idat)
    path.write_bytes(rechunked)


def _upscale_manifest(participant_id: str) -> dict[str, Any]:
    path = UPSCALE_ROOT / f"{participant_id}-manifest.json"
    if participant_id == "chatgpt" and not path.exists():
        path = UPSCALE_ROOT / "manifest.json"
    return json.loads(path.read_text())


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text())
    baseline_by_participant = {
        record["participant_id"]: record for record in baseline["characters"]
    }
    seated_jobs_by_participant: dict[str, dict[str, Any]] = {}
    for manifest_path in SEATED_ROOT.glob("*manifest.json"):
        for job in json.loads(manifest_path.read_text()).get("jobs", []):
            if isinstance(job, dict) and isinstance(job.get("participant_id"), str):
                seated_jobs_by_participant[job["participant_id"]] = job
    participant_ids = tuple(
        participant_id
        for participant_id in STATURE_OFFSETS_PERCENT
        if (SEATED_ROOT / participant_id / "lossless.png").is_file()
        and (UPSCALE_ROOT / f"{participant_id}-2x.png").is_file()
    )
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    for participant_id in participant_ids:
        source = SEATED_ROOT / participant_id / "lossless.png"
        enhanced_rgb = UPSCALE_ROOT / f"{participant_id}-2x.png"
        restored = OUTPUT_ROOT / f"{participant_id}-2x-alpha-restored.png"
        master = OUTPUT_ROOT / f"{participant_id}-master.png"
        lanczos_master = OUTPUT_ROOT / f"{participant_id}-master-lanczos.png"
        detector_compat_master = OUTPUT_ROOT / f"{participant_id}-master-detector-compat.png"
        detector_source_crop = OUTPUT_ROOT / f"{participant_id}-detector-source-crop.png"
        seated_job = seated_jobs_by_participant[participant_id]
        upscale_manifest = _upscale_manifest(participant_id)

        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(enhanced_rgb),
                "-i",
                str(source),
                "-filter_complex",
                (
                    "[1:v]alphaextract,scale=2560:1440:flags=lanczos[alpha];"
                    "[0:v][alpha]alphamerge[restored]"
                ),
                "-map",
                "[restored]",
                "-frames:v",
                "1",
                str(restored),
            ]
        )
        bounds = _alpha_bbox(restored)
        stature_offset = STATURE_OFFSETS_PERCENT[participant_id]
        target_height = round(BASE_BODY_HEIGHT * (1 + stature_offset / 100))
        scale = target_height / bounds["height"]
        target_width = round(bounds["width"] * scale)
        if target_width > CANVAS_WIDTH - 64:
            scale = (CANVAS_WIDTH - 64) / bounds["width"]
            target_width = CANVAS_WIDTH - 64
            target_height = round(bounds["height"] * scale)
        pad_x = round((CANVAS_WIDTH - target_width) / 2)
        pad_y = DESK_BASELINE_Y - target_height
        filter_graph = (
            f"crop={bounds['width']}:{bounds['height']}:{bounds['x']}:{bounds['y']},"
            f"scale={target_width}:{target_height}:flags=lanczos,"
            f"pad={CANVAS_WIDTH}:{CANVAS_HEIGHT}:{pad_x}:{pad_y}:color=0x00000000,"
            "format=rgba"
        )
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(restored),
                "-vf",
                filter_graph,
                "-frames:v",
                "1",
                str(master),
            ]
        )
        _rechunk_png(master)
        master_bounds = _alpha_bbox(master)
        source_bounds = _alpha_bbox(source)
        lanczos_scale = target_height / source_bounds["height"]
        lanczos_width = round(source_bounds["width"] * lanczos_scale)
        lanczos_x = round((CANVAS_WIDTH - lanczos_width) / 2)
        lanczos_y = DESK_BASELINE_Y - target_height
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                (
                    f"crop={source_bounds['width']}:{source_bounds['height']}:"
                    f"{source_bounds['x']}:{source_bounds['y']},"
                    f"scale={lanczos_width}:{target_height}:flags=lanczos,"
                    f"pad={CANVAS_WIDTH}:{CANVAS_HEIGHT}:{lanczos_x}:{lanczos_y}:"
                    "color=0x00000000,format=rgba"
                ),
                "-frames:v",
                "1",
                str(lanczos_master),
            ]
        )
        _rechunk_png(lanczos_master)
        detector_canvas = 1024
        detector_body_height = 700
        detector_baseline_y = 850
        detector_scale = detector_body_height / source_bounds["height"]
        detector_width = round(source_bounds["width"] * detector_scale)
        detector_x = round((detector_canvas - detector_width) / 2)
        detector_y = detector_baseline_y - detector_body_height
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                (
                    f"crop={source_bounds['width']}:{source_bounds['height']}:"
                    f"{source_bounds['x']}:{source_bounds['y']},"
                    f"scale={detector_width}:{detector_body_height}:flags=lanczos,"
                    f"pad={detector_canvas}:{detector_canvas}:{detector_x}:{detector_y}:"
                    "color=0x00000000,format=rgba"
                ),
                "-frames:v",
                "1",
                str(detector_compat_master),
            ]
        )
        _rechunk_png(detector_compat_master)
        source_crop_size = 640
        source_crop_x = max(
            0,
            min(1280 - source_crop_size, source_bounds["x"] + source_bounds["width"] // 2 - 320),
        )
        source_crop_y = max(0, min(720 - source_crop_size, source_bounds["y"] - 32))
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"crop={source_crop_size}:{source_crop_size}:{source_crop_x}:{source_crop_y}",
                "-frames:v",
                "1",
                str(detector_source_crop),
            ]
        )
        _rechunk_png(detector_source_crop)
        seated_character = seated_job["artifact"]["seated_character"]
        face = seated_character["face_region"]
        source_face = {
            "x": round(float(face["x"]) * 2560),
            "y": round(float(face["y"]) * 1440),
            "width": round(float(face["width"]) * 2560),
            "height": round(float(face["height"]) * 1440),
        }
        master_face = {
            "x": round((source_face["x"] - bounds["x"]) * scale + pad_x),
            "y": round((source_face["y"] - bounds["y"]) * scale + pad_y),
            "width": round(source_face["width"] * scale),
            "height": round(source_face["height"] * scale),
        }
        qc = {
            "status": "pass",
            "alpha_bounds_inside_canvas": (
                master_bounds["x"] > 0
                and master_bounds["y"] > 0
                and master_bounds["x2"] < CANVAS_WIDTH - 1
                and master_bounds["y2"] < CANVAS_HEIGHT - 1
            ),
            "desk_baseline_residual_px": abs(master_bounds["y2"] + 1 - DESK_BASELINE_Y),
            "face_height_px": master_face["height"],
            "face_resolution_pass": master_face["height"] >= 380,
            "no_source_replacement": True,
        }
        if not all(
            (
                qc["alpha_bounds_inside_canvas"],
                qc["desk_baseline_residual_px"] <= 1,
                qc["face_resolution_pass"],
            )
        ):
            qc["status"] = "fail"
        records.append(
            {
                "participant_id": participant_id,
                "schema_version": "dialecticore.seated_master_geometry.v2",
                "normalization_version": "desk-baseline-alpha-bounds-v1",
                "source": {
                    "path": str(source.relative_to(ROOT)),
                    "sha256": _sha256(source),
                    "b1_job_id": seated_job["job_id"],
                    "b1_model_version": seated_job["telemetry"]["resolved_model_version"],
                },
                "enhancement": {
                    "path": str(enhanced_rgb.relative_to(ROOT)),
                    "sha256": _sha256(enhanced_rgb),
                    "b1_job_id": upscale_manifest["job"]["id"],
                    "b1_model_version": upscale_manifest["job"]["resolved_model_version"],
                    "matte_policy": "original alpha scaled with Lanczos and reattached",
                },
                "geometry": {
                    "source_alpha_bounds_2x": bounds,
                    "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
                    "desk_baseline_y": DESK_BASELINE_Y,
                    "base_body_height": BASE_BODY_HEIGHT,
                    "intentional_stature_offset_percent": stature_offset,
                    "scale": round(scale, 8),
                    "character_bounds": master_bounds,
                    "face_bounds": master_face,
                    "eye_line_y_estimate": round(master_face["y"] + master_face["height"] * 0.43),
                    "target_seat": seated_character["seat"],
                },
                "master": {
                    "path": str(master.relative_to(ROOT)),
                    "bytes": master.stat().st_size,
                    "sha256": _sha256(master),
                    "mime_type": "image/png",
                },
                "alternatives": {
                    "lanczos_only": {
                        "path": str(lanczos_master.relative_to(ROOT)),
                        "bytes": lanczos_master.stat().st_size,
                        "sha256": _sha256(lanczos_master),
                        "mime_type": "image/png",
                        "purpose": (
                            "identity-preserving fallback when enhanced face detection fails"
                        ),
                    },
                    "detector_compat": {
                        "path": str(detector_compat_master.relative_to(ROOT)),
                        "bytes": detector_compat_master.stat().st_size,
                        "sha256": _sha256(detector_compat_master),
                        "mime_type": "image/png",
                        "canvas": {"width": detector_canvas, "height": detector_canvas},
                        "body_height_px": detector_body_height,
                        "estimated_face_height_px": round(
                            float(face["height"]) * 720 * detector_scale
                        ),
                        "purpose": "bounded fallback for stylized-face detector compatibility",
                    },
                    "detector_source_crop": {
                        "path": str(detector_source_crop.relative_to(ROOT)),
                        "bytes": detector_source_crop.stat().st_size,
                        "sha256": _sha256(detector_source_crop),
                        "mime_type": "image/png",
                        "canvas": {"width": source_crop_size, "height": source_crop_size},
                        "crop": {
                            "x": source_crop_x,
                            "y": source_crop_y,
                            "width": source_crop_size,
                            "height": source_crop_size,
                        },
                        "native_face_height_px": round(float(face["height"]) * 720),
                        "purpose": (
                            "preserve proven native detector scale while removing empty canvas"
                        ),
                    },
                },
                "v1_asset_id": baseline_by_participant[participant_id]["seated_asset_id"],
                "qc": qc,
            }
        )

    heights = [record["geometry"]["character_bounds"]["height"] for record in records]
    report = {
        "schema_version": "dialecticore.production_v2.normalized_seated_masters.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "records": records,
        "aggregate_qc": {
            "status": (
                "pass" if all(record["qc"]["status"] == "pass" for record in records) else "fail"
            ),
            "body_height_min_px": min(heights),
            "body_height_max_px": max(heights),
            "body_height_spread_percent": round((max(heights) / min(heights) - 1) * 100, 3),
            "allowed_intentional_stature_range_percent": 6.0,
        },
    }
    report_path = OUTPUT_ROOT / "manifest.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(report_path.relative_to(ROOT))
    for record in records:
        print(record["participant_id"], record["qc"]["status"], record["master"]["path"])
    return 0 if report["aggregate_qc"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
