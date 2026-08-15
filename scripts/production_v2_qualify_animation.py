#!/usr/bin/env python3
"""Compare v1 plates and normalized v2 masters through B1 MuseTalk."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "output/production-v2/v1-character-quality-baseline.json"
MASTER_ROOT = ROOT / "output/production-v2/normalized-seated-masters"
OUTPUT_ROOT = ROOT / "output/production-v2/animation-qualification"
CA_CERT = ROOT / "storage/runtime-state/certificates/b1-ai-hub-caddy-root.crt"
EPISODE_ID = "cc1ad449-9cad-4a40-a150-652db0b7dc7a"
PARTICIPANTS = ("chatgpt", "claude", "deepseek", "gemini", "grok", "mistral")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _object_path(uri: str) -> Path:
    prefix = "object://dialecticore/"
    if not uri.startswith(prefix):
        raise RuntimeError(f"unsupported private object URI: {uri}")
    return ROOT / "storage/object-store/dialecticore" / uri.removeprefix(prefix)


def _upload(client: httpx.Client, path: Path, *, field: str, content_type: str) -> dict[str, Any]:
    payload = path.read_bytes()
    response = client.post(
        "/v1/media/uploads",
        content=payload,
        headers={"content-type": content_type, "x-b1-field": field},
    )
    response.raise_for_status()
    body = response.json()
    reference = body.get("reference")
    reference_id = reference.get("id") if isinstance(reference, dict) else reference
    if not isinstance(reference_id, str) or not reference_id:
        raise RuntimeError(f"upload did not return a reference id: {body}")
    return {
        "id": reference_id,
        "bytes": len(payload),
        "sha256": _sha256(payload),
        "mime_type": content_type,
        "warning": body.get("warning"),
    }


def _excerpt(source: Path, output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            "4",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not prepare audio excerpt: {completed.stderr[-1000:]}")
    with wave.open(str(output), "rb") as wav_file:
        return round(wav_file.getnframes() / wav_file.getframerate() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--participants", nargs="+", choices=PARTICIPANTS, default=PARTICIPANTS)
    parser.add_argument(
        "--candidates",
        nargs="+",
        choices=(
            "v1_plate",
            "v2_normalized_master",
            "v2_normalized_lanczos",
            "v2_detector_compat",
            "v2_detector_source_crop",
        ),
        default=("v1_plate", "v2_normalized_master"),
    )
    parser.add_argument("--report-name", default="manifest.json")
    args = parser.parse_args()
    token = os.environ.get("B1_API_KEY", "").strip()
    if not token:
        raise SystemExit("B1_API_KEY is required")
    if not CA_CERT.is_file():
        raise SystemExit(f"B1 CA certificate does not exist: {CA_CERT}")
    baseline = json.loads(BASELINE_PATH.read_text())
    baseline_by_participant = {
        record["participant_id"]: record
        for record in baseline["characters"]
        if record["participant_id"] in args.participants
    }
    episode_response = httpx.get(f"http://127.0.0.1:8000/api/v1/episodes/{EPISODE_ID}", timeout=30)
    episode_response.raise_for_status()
    episode = episode_response.json()
    assets = {asset["id"]: asset for asset in episode["assets"]}
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    prepared: dict[str, dict[str, Any]] = {}
    for participant_id in args.participants:
        baseline_record = baseline_by_participant[participant_id]
        speaking_asset = assets[baseline_record["representative_speaking_asset_id"]]
        metadata = speaking_asset["generation_metadata"]
        audio_asset = assets[metadata["audio_asset_id"]]
        audio_source = _object_path(audio_asset["storage_uri"])
        audio_excerpt = OUTPUT_ROOT / participant_id / "excerpt.wav"
        duration_ms = _excerpt(audio_source, audio_excerpt)
        prepared[participant_id] = {
            "audio_source": audio_source,
            "audio_excerpt": audio_excerpt,
            "duration_ms": duration_ms,
            "performance_plan": {
                "schema_version": "dialecticore.character_performance.v1",
                **metadata["prompt_inputs"]["performance"],
            },
            "candidates": {
                "v1_plate": ROOT / baseline_record["seated_plate"]["relative_path"],
                "v2_normalized_master": MASTER_ROOT / f"{participant_id}-master.png",
                "v2_normalized_lanczos": (MASTER_ROOT / f"{participant_id}-master-lanczos.png"),
                "v2_detector_compat": (
                    MASTER_ROOT / f"{participant_id}-master-detector-compat.png"
                ),
                "v2_detector_source_crop": (
                    MASTER_ROOT / f"{participant_id}-detector-source-crop.png"
                ),
            },
        }

    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    report: dict[str, Any] = {
        "schema_version": "dialecticore.production_v2.animation_qualification.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "model": "talking-head-lipsync",
        "configuration": {
            "width": 1024,
            "height": 1024,
            "fps": 12,
            "duration_ms": 4000,
            "mode": "generic portrait input for controlled source comparison",
        },
        "jobs": [],
    }
    started = time.monotonic()
    with httpx.Client(
        base_url="https://api.ai.b1.germering",
        headers=headers,
        timeout=httpx.Timeout(120),
        verify=str(CA_CERT),
    ) as client:
        for participant_id in args.participants:
            item = prepared[participant_id]
            audio_upload = _upload(
                client,
                item["audio_excerpt"],
                field="audio",
                content_type="audio/wav",
            )
            for candidate_id in args.candidates:
                image_path = item["candidates"][candidate_id]
                image_upload = _upload(
                    client,
                    image_path,
                    field="portrait",
                    content_type="image/png",
                )
                request = {
                    "modality": "video",
                    "operation": "talking-head-lipsync",
                    "model": "talking-head-lipsync",
                    "input": {
                        "portrait_artifact_id": image_upload["id"],
                        "audio_artifact_id": audio_upload["id"],
                        "audio_sha256": audio_upload["sha256"],
                        "width": 1024,
                        "height": 1024,
                        "fps": 12,
                        "duration_ms": item["duration_ms"],
                        "performance_plan": item["performance_plan"],
                    },
                    "priority": "video",
                    "runtime_policy": "any",
                }
                idempotency = (
                    f"dialecticore-production-v2-animation-{participant_id}-{candidate_id}-"
                    f"{image_upload['id'][-8:]}-{audio_upload['id'][-8:]}-v1"
                )
                response = client.post(
                    "/v1/media/jobs",
                    json=request,
                    headers={"Idempotency-Key": idempotency},
                )
                response.raise_for_status()
                job = response.json()
                job_id = job.get("id") or job.get("job_id")
                if not isinstance(job_id, str):
                    raise RuntimeError(f"job submission did not return an id: {job}")
                report["jobs"].append(
                    {
                        "participant_id": participant_id,
                        "candidate_id": candidate_id,
                        "job_id": job_id,
                        "state": job.get("state"),
                        "request": request,
                        "image": {
                            "path": str(image_path.relative_to(ROOT)),
                            "upload": image_upload,
                        },
                        "audio": {
                            "path": str(item["audio_excerpt"].relative_to(ROOT)),
                            "upload": audio_upload,
                        },
                    }
                )
                print(participant_id, candidate_id, job_id, flush=True)

        pending = {record["job_id"]: record for record in report["jobs"]}
        while pending:
            if time.monotonic() - started > 1800:
                raise TimeoutError(f"timed out with pending jobs: {sorted(pending)}")
            for job_id, record in list(pending.items()):
                response = client.get(f"/v1/media/jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                record.update(
                    {
                        "state": job.get("state"),
                        "stage": job.get("stage"),
                        "progress": job.get("progress"),
                        "runtime": job.get("runtime"),
                    }
                )
                if job.get("state") not in {"completed", "failed", "cancelled"}:
                    continue
                record["telemetry"] = {
                    key: job.get(key)
                    for key in (
                        "resolved_model_version",
                        "started_at",
                        "completed_at",
                        "load_time_ms",
                        "run_time_ms",
                        "peak_vram_mib",
                        "peak_ram_mib",
                        "failure_category",
                        "failure_message",
                        "lip_sync",
                        "performance",
                    )
                }
                artifacts = job.get("artifacts") or []
                if job.get("state") == "completed" and artifacts:
                    artifact = artifacts[0]
                    artifact_response = client.get(str(artifact["url"]).lstrip("/"))
                    artifact_response.raise_for_status()
                    output_path = (
                        OUTPUT_ROOT / record["participant_id"] / f"{record['candidate_id']}.mp4"
                    )
                    output_path.write_bytes(artifact_response.content)
                    record["artifact"] = {
                        **artifact,
                        "local_path": str(output_path.relative_to(ROOT)),
                        "downloaded_bytes": len(artifact_response.content),
                        "downloaded_sha256": _sha256(artifact_response.content),
                    }
                pending.pop(job_id)
            if pending:
                time.sleep(5)

    report["completed_at"] = datetime.now(UTC).isoformat()
    report["wall_time_seconds"] = round(time.monotonic() - started, 3)
    report_path = OUTPUT_ROOT / args.report_name
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(report_path.relative_to(ROOT))
    return 0 if all(job["state"] == "completed" for job in report["jobs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
