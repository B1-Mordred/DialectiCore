#!/usr/bin/env python3
"""Analyze the v2 animation qualification outputs and record the selected path."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "output/production-v2/animation-qualification"
MANIFESTS = (
    OUTPUT_ROOT / "manifest.json",
    OUTPUT_ROOT / "deepseek-lanczos-manifest.json",
    OUTPUT_ROOT / "deepseek-detector-compat-manifest.json",
    OUTPUT_ROOT / "deepseek-detector-source-crop-manifest.json",
    OUTPUT_ROOT / "remaining-master-manifest.json",
)
SELECTED = {
    "chatgpt": "v2_normalized_master",
    "claude": "v2_normalized_master",
    "deepseek": "v2_detector_source_crop",
    "gemini": "v2_normalized_master",
    "grok": "v2_normalized_master",
    "mistral": "v2_normalized_master",
}
VISUAL_REVIEW = {
    "chatgpt": "enhanced master is speaker-sized, sharp, identity-stable, and visibly articulates",
    "claude": "enhanced master is speaker-sized, sharp, identity-stable, and visibly articulates",
    "deepseek": (
        "native-scale source crop is substantially larger than v1 and visibly "
        "articulates without detector failure"
    ),
    "gemini": "enhanced master is clean, speaker-sized, and visibly articulates",
    "grok": "enhanced master is clean, speaker-sized, and visibly articulates",
    "mistral": (
        "enhanced master is clean and visibly articulates; narrow silhouette retains "
        "more torso context"
    ),
}
NUMBER_RE = re.compile(r"(?:average:|All:)([0-9.]+)")
MOTION_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}\n{output[-1500:]}")
    return output


def _probe(path: Path) -> dict[str, Any]:
    return json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,codec_type,width,height,pix_fmt,avg_frame_rate,bit_rate:"
                "format=duration,size,bit_rate",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def _filter_metric(path: Path, filter_name: str) -> float | None:
    output = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-t",
            "3",
            "-i",
            str(path),
            "-vf",
            filter_name,
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    matches = NUMBER_RE.findall(output)
    return round(float(matches[-1]), 6) if matches else None


def _motion(path: Path) -> dict[str, float]:
    output = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            "tblend=all_mode=difference,signalstats,metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    values = [float(value) for value in MOTION_RE.findall(output)]
    if not values:
        raise RuntimeError(f"no frame-motion values found for {path}")
    return {
        "mean_luma_difference": round(statistics.mean(values), 6),
        "median_luma_difference": round(statistics.median(values), 6),
        "max_luma_difference": round(max(values), 6),
    }


def main() -> int:
    jobs: list[dict[str, Any]] = []
    for manifest_path in MANIFESTS:
        manifest = json.loads(manifest_path.read_text())
        for job in manifest["jobs"]:
            jobs.append({**job, "source_manifest": str(manifest_path.relative_to(ROOT))})

    analyzed: list[dict[str, Any]] = []
    for job in jobs:
        artifact = job.get("artifact")
        local_path = artifact.get("local_path") if isinstance(artifact, dict) else None
        analysis: dict[str, Any] = {
            "participant_id": job["participant_id"],
            "candidate_id": job["candidate_id"],
            "job_id": job["job_id"],
            "state": job["state"],
            "selected": SELECTED.get(job["participant_id"]) == job["candidate_id"],
            "source_manifest": job["source_manifest"],
            "run_time_ms": job.get("telemetry", {}).get("run_time_ms"),
            "peak_vram_mib": job.get("telemetry", {}).get("peak_vram_mib"),
            "peak_ram_mib": job.get("telemetry", {}).get("peak_ram_mib"),
            "failure_category": job.get("telemetry", {}).get("failure_category"),
            "failure_message": job.get("telemetry", {}).get("failure_message"),
        }
        if isinstance(local_path, str):
            path = ROOT / local_path
            analysis.update(
                {
                    "path": local_path,
                    "sha256": artifact["downloaded_sha256"],
                    "probe": _probe(path),
                    "block_mean_first_3s": _filter_metric(path, "blockdetect=period_min=8"),
                    "blur_mean_first_3s": _filter_metric(path, "blurdetect"),
                    "frame_motion": _motion(path),
                }
            )
        analyzed.append(analysis)

    selected_records = [record for record in analyzed if record["selected"]]
    report = {
        "schema_version": "dialecticore.production_v2.animation_analysis.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "selection": SELECTED,
        "selection_rationale": {
            "policy": (
                "prefer enhanced normalized masters; retain native detector scale and crop "
                "empty canvas only when the pinned MuseTalk face detector rejects enhancement"
            ),
            "visual_review": VISUAL_REVIEW,
            "deepseek_rejected_candidates": [
                "v2_normalized_master: lipsync_face_not_detected",
                "v2_normalized_lanczos: lipsync_face_not_detected",
                "v2_detector_compat: lipsync_face_not_detected",
            ],
        },
        "records": analyzed,
        "aggregate": {
            "selected_count": len(selected_records),
            "selected_completed_count": sum(
                record["state"] == "completed" for record in selected_records
            ),
            "selected_peak_vram_mib": max(
                int(record["peak_vram_mib"] or 0) for record in selected_records
            ),
            "selected_peak_ram_mib": max(
                int(record["peak_ram_mib"] or 0) for record in selected_records
            ),
            "all_selected_audio_driven": all(
                record["state"] == "completed" for record in selected_records
            ),
        },
    }
    json_path = OUTPUT_ROOT / "analysis.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    markdown = [
        "# Production v2 animation qualification",
        "",
        f"Generated: {report['created_at']}",
        "",
        (
            "| Character | Selected input | State | Runtime ms | Peak VRAM MiB | "
            "Block | Blur | Motion mean |"
        ),
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for record in selected_records:
        markdown.append(
            "| {participant_id} | {candidate_id} | {state} | {runtime} | {vram} | "
            "{block} | {blur} | {motion} |".format(
                **record,
                runtime=record["run_time_ms"] or "n/a",
                vram=record["peak_vram_mib"] or "n/a",
                block=record.get("block_mean_first_3s", "n/a"),
                blur=record.get("blur_mean_first_3s", "n/a"),
                motion=record.get("frame_motion", {}).get("mean_luma_difference", "n/a"),
            )
        )
    markdown.extend(
        [
            "",
            "Five characters use the enhanced normalized master. DeepSeek uses the native-scale "
            "source crop because all three enlarged inputs failed the pinned MuseTalk face "
            "detector. The selected crop still removes empty canvas and produces "
            "substantially larger framing.",
            "",
            f"Peak selected VRAM: {report['aggregate']['selected_peak_vram_mib']} MiB. "
            f"Peak selected host RAM: {report['aggregate']['selected_peak_ram_mib']} MiB.",
        ]
    )
    markdown_path = OUTPUT_ROOT / "analysis.md"
    markdown_path.write_text("\n".join(markdown) + "\n")
    print(json_path.relative_to(ROOT))
    print(markdown_path.relative_to(ROOT))
    return 0 if report["aggregate"]["selected_completed_count"] == len(SELECTED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
