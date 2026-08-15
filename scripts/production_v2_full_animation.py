#!/usr/bin/env python3
"""Generate all production-v2 speaking turns through B1 managed MuseTalk."""

from __future__ import annotations

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
SOURCE_EPISODE_ID = "cc1ad449-9cad-4a40-a150-652db0b7dc7a"
OUTPUT_ROOT = ROOT / "output/production-v2/full-production/animation"
MANIFEST_PATH = OUTPUT_ROOT / "manifest.json"
MASTER_ROOT = ROOT / "output/production-v2/normalized-seated-masters"
BASELINE_PATH = ROOT / "output/production-v2/v1-character-quality-baseline.json"
CA_CERT = ROOT / "storage/runtime-state/certificates/b1-ai-hub-caddy-root.crt"
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _object_path(uri: str) -> Path:
    prefix = "object://dialecticore/"
    if not uri.startswith(prefix):
        raise RuntimeError(f"unsupported private object URI: {uri}")
    return ROOT / "storage/object-store/dialecticore" / uri.removeprefix(prefix)


def _master_path(participant_id: str) -> Path:
    suffix = "detector-source-crop.png" if participant_id == "deepseek" else "master.png"
    return MASTER_ROOT / f"{participant_id}-{suffix}"


def _normalized_audio_path(source: Path, turn_id: str) -> tuple[Path, int]:
    """Write a seekable PCM WAV whose header reports the real frame count.

    Some Voicebox streaming WAVs use a sentinel data-chunk length. FFmpeg can
    play those correctly, but Python's wave module (also used by B1's input
    validator) interprets the sentinel as a multi-hour file.
    """
    target = OUTPUT_ROOT / "input-audio" / f"{turn_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    source_sha256 = _sha256(source.read_bytes())
    stamp = target.with_suffix(".source-sha256")
    if not target.is_file() or not stamp.is_file() or stamp.read_text().strip() != source_sha256:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            check=True,
        )
        stamp.write_text(source_sha256 + "\n")
    with wave.open(str(target), "rb") as wav:
        duration_ms = round(wav.getnframes() * 1000 / wav.getframerate())
    return target, duration_ms


def _upload(
    client: httpx.Client,
    path: Path,
    *,
    field: str,
    content_type: str,
) -> dict[str, Any]:
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


def _write_manifest(manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _download_first_artifact(
    client: httpx.Client, record: dict[str, Any], artifacts: list[dict[str, Any]]
) -> None:
    if not artifacts:
        raise RuntimeError(f"completed job {record['job_id']} has no artifact")
    artifact = artifacts[0]
    download = client.get(str(artifact["url"]).lstrip("/"))
    download.raise_for_status()
    output_path = ROOT / record["artifact_path"]
    output_path.write_bytes(download.content)
    record["artifact"] = {
        **artifact,
        "downloaded_bytes": len(download.content),
        "downloaded_sha256": _sha256(download.content),
    }


def _update_record_from_job(record: dict[str, Any], job: dict[str, Any]) -> None:
    record.update(
        {
            "state": job.get("state"),
            "stage": job.get("stage"),
            "progress": job.get("progress"),
            "runtime": job.get("runtime"),
            "telemetry": {
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
            },
        }
    )


def _source_episode() -> dict[str, Any]:
    response = httpx.get(
        f"http://127.0.0.1:8000/api/v1/episodes/{SOURCE_EPISODE_ID}", timeout=60
    )
    response.raise_for_status()
    return response.json()


def _performance_by_participant(episode: dict[str, Any]) -> dict[str, dict[str, Any]]:
    baseline = json.loads(BASELINE_PATH.read_text())
    assets = {asset["id"]: asset for asset in episode["assets"]}
    result: dict[str, dict[str, Any]] = {}
    for record in baseline["characters"]:
        participant_id = record["participant_id"]
        speaking = assets[record["representative_speaking_asset_id"]]
        performance = speaking.get("generation_metadata", {}).get("prompt_inputs", {}).get(
            "performance", {}
        )
        result[participant_id] = {
            "schema_version": "dialecticore.character_performance.v1",
            **performance,
        }
    return result


def _planned_turns(episode: dict[str, Any]) -> list[dict[str, Any]]:
    transcript_id = episode["canonical_transcript_version_id"]
    transcript = next(item for item in episode["transcripts"] if item["id"] == transcript_id)
    audio_by_turn = {
        asset["source_entity_id"]: asset
        for asset in episode["assets"]
        if asset["asset_type"] == "audio"
        and asset["status"] == "completed"
        and asset.get("generation_metadata", {}).get("transcript_version_id") == transcript_id
    }
    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(
        (item for item in transcript["turns"] if item.get("status") != "excluded"), start=1
    ):
        audio = audio_by_turn.get(turn["id"])
        if audio is None:
            raise RuntimeError(f"canonical turn {turn['id']} has no completed audio")
        audio_path = _object_path(audio["storage_uri"])
        if not audio_path.is_file():
            raise RuntimeError(f"canonical audio file is missing: {audio_path}")
        master = _master_path(turn["speaker_participant_id"])
        if not master.is_file():
            raise RuntimeError(f"qualified master is missing: {master}")
        upload_audio_path, upload_duration_ms = _normalized_audio_path(
            audio_path, turn["id"]
        )
        turns.append(
            {
                "index": index,
                "turn_id": turn["id"],
                "participant_id": turn["speaker_participant_id"],
                "turn_type": turn.get("turn_type"),
                "text": turn["text"],
                "audio_asset_id": audio["id"],
                "source_audio_path": str(audio_path.relative_to(ROOT)),
                "source_audio_sha256": _sha256(audio_path.read_bytes()),
                "audio_path": str(upload_audio_path.relative_to(ROOT)),
                "audio_sha256": _sha256(upload_audio_path.read_bytes()),
                "duration_ms": upload_duration_ms,
                "master_path": str(master.relative_to(ROOT)),
                "master_sha256": _sha256(master.read_bytes()),
            }
        )
    return turns


def main() -> int:
    token = os.environ.get("B1_API_KEY", "").strip()
    if not token:
        raise SystemExit("B1_API_KEY is required; load the repository .env before running")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    episode = _source_episode()
    planned_turns = _planned_turns(episode)
    performance = _performance_by_participant(episode)
    if MANIFEST_PATH.is_file():
        manifest = json.loads(MANIFEST_PATH.read_text())
    else:
        manifest = {
            "schema_version": "dialecticore.production_v2.full_animation.v1",
            "source_episode_id": SOURCE_EPISODE_ID,
            "source_transcript_version_id": episode["canonical_transcript_version_id"],
            "created_at": datetime.now(UTC).isoformat(),
            "configuration": {
                "model": "talking-head-lipsync",
                "width": 1024,
                "height": 1024,
                "fps": 12,
                "runtime_policy": "any",
                "scheduler": "B1 managed media API",
                "direct_backend_access": False,
            },
            "jobs": [],
        }
    jobs_by_turn = {job["turn_id"]: job for job in manifest["jobs"]}
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    started = time.monotonic()
    with httpx.Client(
        base_url="https://api.ai.b1.germering",
        headers=headers,
        timeout=httpx.Timeout(180),
        verify=str(CA_CERT),
    ) as client:
        # Reconcile a prior interrupted monitor before deciding what to resubmit.
        for record in manifest["jobs"]:
            response = client.get(f"/v1/media/jobs/{record['job_id']}")
            if response.status_code == 404:
                record["state"] = "failed"
                record["failure_message"] = "B1 job record is no longer available"
                continue
            response.raise_for_status()
            job = response.json()
            _update_record_from_job(record, job)
            if job.get("state") == "completed":
                output_path = ROOT / record["artifact_path"]
                local_sha256 = (
                    _sha256(output_path.read_bytes()) if output_path.is_file() else None
                )
                if local_sha256 != record.get("artifact", {}).get("downloaded_sha256"):
                    _download_first_artifact(client, record, job.get("artifacts") or [])
        _write_manifest(manifest)

        portrait_uploads: dict[str, dict[str, Any]] = {}
        for turn in planned_turns:
            existing = jobs_by_turn.get(turn["turn_id"])
            if existing is not None:
                # Older resumable records may predate upload-only WAV
                # normalization. Preserve the exact submitted input fields, but
                # backfill immutable canonical-audio provenance.
                existing.setdefault("source_audio_path", turn["source_audio_path"])
                existing.setdefault(
                    "source_audio_sha256", turn["source_audio_sha256"]
                )
            artifact_path = OUTPUT_ROOT / (
                f"{turn['index']:02d}-{turn['turn_id']}-{turn['participant_id']}.mp4"
            )
            if (
                existing is not None
                and existing.get("state") == "completed"
                and artifact_path.is_file()
                and existing.get("artifact", {}).get("downloaded_sha256")
                == _sha256(artifact_path.read_bytes())
            ):
                continue
            if existing is not None and existing.get("state") not in TERMINAL_STATES:
                continue
            participant_id = turn["participant_id"]
            if participant_id not in portrait_uploads:
                portrait_uploads[participant_id] = _upload(
                    client,
                    ROOT / turn["master_path"],
                    field="portrait",
                    content_type="image/png",
                )
            audio_upload = _upload(
                client,
                ROOT / turn["audio_path"],
                field="audio",
                content_type="audio/wav",
            )
            request = {
                "modality": "video",
                "operation": "talking-head-lipsync",
                "model": "talking-head-lipsync",
                "input": {
                    "portrait_artifact_id": portrait_uploads[participant_id]["id"],
                    "audio_artifact_id": audio_upload["id"],
                    "audio_sha256": audio_upload["sha256"],
                    "width": 1024,
                    "height": 1024,
                    "fps": 12,
                    "duration_ms": turn["duration_ms"],
                    "performance_plan": performance[participant_id],
                },
                "priority": "video",
                "runtime_policy": "any",
            }
            idempotency = (
                f"dialecticore-production-v2-full-{turn['turn_id']}-"
                f"{turn['audio_sha256'][:12]}-{turn['master_sha256'][:12]}-v2"
            )
            response = client.post(
                "/v1/media/jobs",
                json=request,
                headers={"Idempotency-Key": idempotency},
            )
            response.raise_for_status()
            submitted = response.json()
            job_id = submitted.get("id") or submitted.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise RuntimeError(f"job submission did not return an id: {submitted}")
            record = {
                **turn,
                "job_id": job_id,
                "state": submitted.get("state"),
                "request": request,
                "idempotency_key": idempotency,
                "portrait_upload": portrait_uploads[participant_id],
                "audio_upload": audio_upload,
                "artifact_path": str(artifact_path.relative_to(ROOT)),
            }
            if existing is None:
                manifest["jobs"].append(record)
            else:
                existing.clear()
                existing.update(record)
            jobs_by_turn[turn["turn_id"]] = record
            _write_manifest(manifest)
            print(f"submitted {turn['index']:02d}/21 {participant_id} {job_id}", flush=True)

        pending = {
            job["job_id"]: job
            for job in manifest["jobs"]
            if job.get("state") not in TERMINAL_STATES
        }
        while pending:
            if time.monotonic() - started > 21_600:
                raise TimeoutError(f"timed out with pending jobs: {sorted(pending)}")
            for job_id, record in list(pending.items()):
                response = client.get(f"/v1/media/jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                _update_record_from_job(record, job)
                if job.get("state") not in TERMINAL_STATES:
                    continue
                artifacts = job.get("artifacts") or []
                if job.get("state") == "completed" and artifacts:
                    _download_first_artifact(client, record, artifacts)
                pending.pop(job_id)
                print(
                    f"terminal {record['index']:02d}/21 {record['participant_id']} "
                    f"{record['state']}",
                    flush=True,
                )
                _write_manifest(manifest)
            if pending:
                states: dict[str, int] = {}
                for record in pending.values():
                    state = str(record.get("state") or "unknown")
                    states[state] = states.get(state, 0) + 1
                print(f"waiting {len(pending)} {states}", flush=True)
                time.sleep(10)

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["wall_time_seconds"] = round(time.monotonic() - started, 3)
    manifest["summary"] = {
        "turn_count": len(manifest["jobs"]),
        "completed_count": sum(job.get("state") == "completed" for job in manifest["jobs"]),
        "failed_count": sum(job.get("state") == "failed" for job in manifest["jobs"]),
        "cancelled_count": sum(job.get("state") == "cancelled" for job in manifest["jobs"]),
    }
    _write_manifest(manifest)
    print(json.dumps(manifest["summary"], indent=2))
    return 0 if manifest["summary"]["completed_count"] == len(planned_turns) else 1


if __name__ == "__main__":
    raise SystemExit(main())
