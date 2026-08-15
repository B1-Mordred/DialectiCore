#!/usr/bin/env python3
"""Run deterministic technical QC for the complete Production v2 preview."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RENDER_ROOT = ROOT / "output/production-v2/full-production/render"
ANIMATION_MANIFEST = (
    ROOT / "output/production-v2/full-production/animation/manifest.json"
)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(path: Path) -> dict[str, Any]:
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
    return json.loads(completed.stdout)


def _contact_sheet(preview: Path, timeline: dict[str, Any], output: Path) -> None:
    frames = RENDER_ROOT / "qc-frames"
    frames.mkdir(exist_ok=True)
    samples = [
        (int(clip["start_ms"]) + int(clip["end_ms"])) / 2000
        for clip in timeline["tracks"]["dialogue"]
    ]
    for index, timestamp in enumerate(samples, start=1):
        frame = frames / f"{index:02d}.png"
        completed = _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(preview),
                "-frames:v",
                "1",
                str(frame),
            ]
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr[-2000:])
    completed = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-pattern_type",
            "glob",
            "-i",
            str(frames / "*.png"),
            "-filter_complex",
            "scale=320:180:flags=lanczos,tile=4x6:padding=4:margin=4:color=0x111827",
            "-frames:v",
            "1",
            str(output),
        ]
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-2000:])


def main() -> int:
    render_manifest = json.loads((RENDER_ROOT / "manifest.json").read_text())
    animation = json.loads(ANIMATION_MANIFEST.read_text())
    timeline = json.loads((ROOT / render_manifest["timeline"]["path"]).read_text())
    preview = ROOT / render_manifest["preview"]["path"]
    probe = _probe(preview)
    streams = probe.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    actual_duration_ms = round(float(probe["format"]["duration"]) * 1000)
    expected_duration_ms = int(timeline["duration_ms"])
    video_start = float((video or {}).get("start_time") or 0)
    audio_start = float((audio or {}).get("start_time") or 0)
    av_offset_ms = round(abs(video_start - audio_start) * 1000, 3)

    decode = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(preview),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
    )
    silence = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(preview),
            "-af",
            "silencedetect=noise=-50dB:d=1.5",
            "-f",
            "null",
            "-",
        ]
    )
    silence_events = [
        line.strip()
        for line in silence.stderr.splitlines()
        if "silence_start:" in line or "silence_end:" in line
    ]
    jobs = animation["jobs"]
    missing_artifacts = [
        int(job["index"])
        for job in jobs
        if not (ROOT / job["artifact_path"]).is_file()
        or job.get("artifact", {}).get("downloaded_sha256")
        != _sha256(ROOT / job["artifact_path"])
    ]
    checks = {
        "animation_turn_count": len(jobs) == 21,
        "animation_jobs_completed": all(job.get("state") == "completed" for job in jobs),
        "animation_artifact_integrity": not missing_artifacts,
        "preview_sha256": _sha256(preview) == render_manifest["preview"]["sha256"],
        "preview_duration": abs(actual_duration_ms - expected_duration_ms) <= 50,
        "preview_resolution": video is not None
        and int(video.get("width") or 0) == 1280
        and int(video.get("height") or 0) == 720,
        "preview_video_codec": video is not None and video.get("codec_name") == "h264",
        "preview_audio_present": audio is not None and audio.get("codec_name") == "aac",
        "preview_av_alignment": av_offset_ms <= 50,
        "preview_full_decode": decode.returncode == 0,
        "parallel_track_contract": set(timeline["tracks"])
        >= {
            "dialogue",
            "character_performance",
            "camera_direction",
            "broll_content",
            "broll_presentation",
            "captions",
        },
    }
    contact_sheet = RENDER_ROOT / "production-v2-full-contact-sheet.png"
    _contact_sheet(preview, timeline, contact_sheet)
    failed_checks = [name for name, passed in checks.items() if not passed]
    telemetry = [job.get("telemetry") or {} for job in jobs]
    result = {
        "schema_version": "dialecticore.production_v2.full_qc.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "pass" if not failed_checks else "fail",
        "checks": checks,
        "failed_checks": failed_checks,
        "preview": {
            "path": str(preview.relative_to(ROOT)),
            "sha256": _sha256(preview),
            "expected_duration_ms": expected_duration_ms,
            "actual_duration_ms": actual_duration_ms,
            "duration_delta_ms": actual_duration_ms - expected_duration_ms,
            "av_offset_ms": av_offset_ms,
            "probe": probe,
            "decode_stderr": decode.stderr[-2000:],
        },
        "animation": {
            "turn_count": len(jobs),
            "missing_artifact_indices": missing_artifacts,
            "max_peak_vram_mib": max(
                int(item.get("peak_vram_mib") or 0) for item in telemetry
            ),
            "max_peak_ram_mib": max(
                int(item.get("peak_ram_mib") or 0) for item in telemetry
            ),
            "total_run_time_ms": sum(
                int(item.get("run_time_ms") or 0) for item in telemetry
            ),
        },
        "audio": {
            "silence_detection_threshold": "-50dB for 1.5s",
            "silence_events": silence_events,
            "silence_events_are_review_information": True,
        },
        "contact_sheet": {
            "path": str(contact_sheet.relative_to(ROOT)),
            "sha256": _sha256(contact_sheet),
            "sample_count": len(timeline["tracks"]["dialogue"]),
        },
        "human_review_required": True,
    }
    output = RENDER_ROOT / "qc.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    markdown = [
        "# Production v2 full-preview technical QC",
        "",
        f"Status: **{result['status']}**",
        "",
        f"Preview SHA-256: `{result['preview']['sha256']}`",
        f"Duration: {actual_duration_ms} ms (delta {actual_duration_ms - expected_duration_ms} ms)",
        f"A/V start offset: {av_offset_ms} ms",
        f"Animations: {len(jobs)} completed managed B1 jobs",
        f"Peak P40 VRAM: {result['animation']['max_peak_vram_mib']} MiB",
        f"Peak host RAM: {result['animation']['max_peak_ram_mib']} MiB",
        "",
        "Technical QC does not replace the required human visual and editorial review.",
    ]
    (RENDER_ROOT / "qc.md").write_text("\n".join(markdown) + "\n")
    print(json.dumps({"status": result["status"], "failed_checks": failed_checks}, indent=2))
    return 0 if not failed_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
