#!/usr/bin/env python3
"""Run a controlled lossless-reference seated-character qualification on B1.

The script deliberately writes only under output/production-v2. It submits new
managed B1 jobs and never updates a DialectiCore episode or its approved assets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "output/production-v2/v1-character-quality-baseline.json"
OUTPUT_ROOT = ROOT / "output/production-v2/seated-transport-qualification"
EPISODE_ID = "cc1ad449-9cad-4a40-a150-652db0b7dc7a"
STUDIO_PATH = (
    ROOT / "storage/object-store/dialecticore/show-media/scene-reference-images/"
    "47d9f89bed32daac.png"
)
DEFAULT_CA_CERT = ROOT / "storage/runtime-state/certificates/b1-ai-hub-caddy-root.crt"
ALL_PARTICIPANTS = ("chatgpt", "claude", "deepseek", "gemini", "grok", "mistral")
DEFAULT_SELECTED = ("chatgpt", "claude", "deepseek")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _upload(client: httpx.Client, path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    response = client.post(
        "/v1/media/uploads",
        content=payload,
        headers={"content-type": "image/png", "x-b1-field": "image"},
    )
    response.raise_for_status()
    body = response.json()
    reference = body.get("reference")
    reference_id = reference.get("id") if isinstance(reference, dict) else reference
    if not isinstance(reference_id, str) or not reference_id:
        raise RuntimeError(f"upload did not return a reference id: {body}")
    return {
        "id": reference_id,
        "path": str(path.relative_to(ROOT)),
        "bytes": len(payload),
        "sha256": _sha256(payload),
        "mime_type": "image/png",
        "warning": body.get("warning"),
    }


def _episode_seeds(participant_ids: tuple[str, ...]) -> dict[str, dict[str, int]]:
    response = httpx.get(f"http://127.0.0.1:8000/api/v1/episodes/{EPISODE_ID}", timeout=30)
    response.raise_for_status()
    selected_ids = {
        item["participant_id"]: item["seated_asset_id"]
        for item in json.loads(BASELINE_PATH.read_text())["characters"]
        if item["participant_id"] in participant_ids
    }
    result: dict[str, dict[str, int]] = {}
    for asset in response.json()["assets"]:
        participant_id = asset.get("source_entity_id")
        if asset.get("id") != selected_ids.get(participant_id):
            continue
        managed_input = asset["generation_metadata"]["managed_media_payload"]["input"]
        result[participant_id] = {
            "seed": int(managed_input["seed"]),
            "seat": int(managed_input["seat"]),
        }
    missing = sorted(set(participant_ids) - set(result))
    if missing:
        raise RuntimeError(f"baseline episode is missing managed seated assets: {missing}")
    return result


def _artifact_url(value: str) -> str:
    return value if value.startswith("http") else value.lstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="https://api.ai.b1.germering")
    parser.add_argument(
        "--participants",
        nargs="+",
        choices=ALL_PARTICIPANTS,
        default=DEFAULT_SELECTED,
    )
    parser.add_argument("--ca-cert", type=Path, default=DEFAULT_CA_CERT)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--report-name", default="manifest.json")
    args = parser.parse_args()

    token = os.environ.get("B1_API_KEY", "").strip()
    if not token:
        raise SystemExit("B1_API_KEY is required")
    if not args.ca_cert.is_file():
        raise SystemExit(f"B1 CA certificate does not exist: {args.ca_cert}")
    baseline = json.loads(BASELINE_PATH.read_text())
    participant_ids = tuple(args.participants)
    records = {
        item["participant_id"]: item
        for item in baseline["characters"]
        if item["participant_id"] in participant_ids
    }
    seeds = _episode_seeds(participant_ids)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    manifest: dict[str, Any] = {
        "schema_version": "dialecticore.production_v2.seated_transport_qualification.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "baseline_episode_id": EPISODE_ID,
        "model": "studio-seated-character-p40",
        "controlled_variables": [
            "model",
            "seed",
            "seat",
            "pose",
            "camera_view",
            "camera_angle",
            "width",
            "height",
            "studio_reference",
        ],
        "changed_variable": "portrait and full-body transport preserves original PNG bytes",
        "jobs": [],
    }
    started = time.monotonic()
    with httpx.Client(
        base_url=args.api_base,
        headers=headers,
        timeout=httpx.Timeout(120),
        verify=str(args.ca_cert),
    ) as client:
        studio_upload = _upload(client, STUDIO_PATH)
        for participant_id in participant_ids:
            record = records[participant_id]
            participant_dir = OUTPUT_ROOT / participant_id
            participant_dir.mkdir(parents=True, exist_ok=True)
            portrait = ROOT / record["portrait_reference"]["relative_path"]
            full_body = ROOT / record["full_body_reference"]["relative_path"]
            portrait_upload = _upload(client, portrait)
            full_body_upload = _upload(client, full_body)
            request = {
                "modality": "image",
                "operation": "studio-seated-character",
                "model": "studio-seated-character-p40",
                "input": {
                    "participant_id": participant_id,
                    "portrait_artifact_id": portrait_upload["id"],
                    "full_body_artifact_id": full_body_upload["id"],
                    "studio_reference_artifact_id": studio_upload["id"],
                    "seat": seeds[participant_id]["seat"],
                    "pose": "neutral_seated",
                    "camera_view": "establishing_wide",
                    "camera_angle": "front_three_quarter",
                    "width": 1280,
                    "height": 720,
                    "seed": seeds[participant_id]["seed"],
                },
                "priority": "single_image",
                "runtime_policy": "any",
            }
            response = client.post(
                "/v1/media/jobs",
                json=request,
                headers={
                    "Idempotency-Key": (
                        f"dialecticore-production-v2-lossless-seated-{participant_id}-"
                        f"{portrait_upload['id'][-8:]}-{full_body_upload['id'][-8:]}-v1"
                    )
                },
            )
            response.raise_for_status()
            job = response.json()
            job_id = job.get("id") or job.get("job_id")
            if not isinstance(job_id, str):
                raise RuntimeError(f"job submission did not return an id: {job}")
            manifest["jobs"].append(
                {
                    "participant_id": participant_id,
                    "job_id": job_id,
                    "old_asset_id": record["seated_asset_id"],
                    "old_sha256": record["seated_plate"]["sha256"],
                    "uploads": {
                        "portrait": portrait_upload,
                        "full_body": full_body_upload,
                        "studio": studio_upload,
                    },
                    "request": request,
                    "state": job.get("state"),
                }
            )

        pending = {item["job_id"]: item for item in manifest["jobs"]}
        while pending:
            if time.monotonic() - started > args.timeout_seconds:
                raise TimeoutError(f"timed out with pending jobs: {sorted(pending)}")
            for job_id, record in list(pending.items()):
                response = client.get(f"/v1/media/jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                record.update(
                    {
                        "state": job.get("state"),
                        "progress": job.get("progress"),
                        "stage": job.get("stage"),
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
                    )
                }
                artifacts = job.get("artifacts") or []
                if job.get("state") == "completed" and artifacts:
                    artifact = artifacts[0]
                    artifact_response = client.get(_artifact_url(artifact["url"]))
                    artifact_response.raise_for_status()
                    output_path = OUTPUT_ROOT / record["participant_id"] / "lossless.png"
                    output_path.write_bytes(artifact_response.content)
                    record["artifact"] = {
                        **artifact,
                        "local_path": str(output_path.relative_to(ROOT)),
                        "downloaded_bytes": len(artifact_response.content),
                        "downloaded_sha256": _sha256(artifact_response.content),
                    }
                pending.pop(job_id)
            if pending:
                time.sleep(args.poll_seconds)

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest["wall_time_seconds"] = round(time.monotonic() - started, 3)
    manifest_path = OUTPUT_ROOT / args.report_name
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(manifest_path.relative_to(ROOT))
    for job in manifest["jobs"]:
        print(
            job["participant_id"],
            job["state"],
            job["job_id"],
            job.get("artifact", {}).get("downloaded_sha256", "no-artifact"),
        )
    return 0 if all(item["state"] == "completed" for item in manifest["jobs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
