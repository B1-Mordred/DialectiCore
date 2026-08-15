#!/usr/bin/env python3
"""Qualify B1-managed Real-ESRGAN on one v2 seated-character plate."""

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
DEFAULT_OUTPUT = ROOT / "output/production-v2/upscale-qualification"
DEFAULT_CA_CERT = ROOT / "storage/runtime-state/certificates/b1-ai-hub-caddy-root.crt"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--participant", default="chatgpt")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--api-base", default="https://api.ai.b1.germering")
    parser.add_argument("--ca-cert", type=Path, default=DEFAULT_CA_CERT)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    source_path = args.source or (
        ROOT
        / "output/production-v2/seated-transport-qualification"
        / args.participant
        / "lossless.png"
    )

    token = os.environ.get("B1_API_KEY", "").strip()
    if not token:
        raise SystemExit("B1_API_KEY is required")
    for label, path in (("source", source_path), ("B1 CA certificate", args.ca_cert)):
        if not path.is_file():
            raise SystemExit(f"{label} does not exist: {path}")

    source = source_path.read_bytes()
    payload: dict[str, Any]
    source_upload: dict[str, Any]
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    started = time.monotonic()
    with httpx.Client(
        base_url=args.api_base,
        headers=headers,
        timeout=httpx.Timeout(120),
        verify=str(args.ca_cert),
    ) as client:
        upload_response = client.post(
            "/v1/media/uploads",
            content=source,
            headers={"content-type": "image/png", "x-b1-field": "source_image"},
        )
        upload_response.raise_for_status()
        upload_body = upload_response.json()
        reference = upload_body.get("reference")
        reference_id = reference.get("id") if isinstance(reference, dict) else reference
        if not isinstance(reference_id, str) or not reference_id:
            raise RuntimeError(f"source upload did not return a reference id: {upload_body}")
        source_upload = {
            "id": reference_id,
            "bytes": len(source),
            "sha256": _sha256(source),
            "mime_type": "image/png",
            "warning": upload_body.get("warning"),
        }
        payload = {
            "modality": "image",
            "operation": "upscale",
            "model": "image-upscale",
            "input": {
                "source_image": reference,
                "scale": 2,
                "post_scale": 0.5,
            },
            "priority": "single_image",
            "runtime_policy": "any",
        }
        response = client.post(
            "/v1/media/jobs",
            json=payload,
            headers={
                "Idempotency-Key": (
                    f"dialecticore-production-v2-upscale-{args.participant}-"
                    f"{_sha256(source)[:12]}-v1"
                )
            },
        )
        response.raise_for_status()
        job = response.json()
        job_id = job.get("id") or job.get("job_id")
        if not isinstance(job_id, str):
            raise RuntimeError(f"job submission did not return an id: {job}")
        while job.get("state") not in {"completed", "failed", "cancelled"}:
            if time.monotonic() - started > args.timeout_seconds:
                raise TimeoutError(f"timed out waiting for {job_id}")
            time.sleep(args.poll_seconds)
            response = client.get(f"/v1/media/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()

        args.output.mkdir(parents=True, exist_ok=True)
        artifact_record: dict[str, Any] | None = None
        artifacts = job.get("artifacts") or []
        if job.get("state") == "completed" and artifacts:
            artifact = artifacts[0]
            artifact_response = client.get(str(artifact["url"]).lstrip("/"))
            artifact_response.raise_for_status()
            output_path = args.output / f"{args.participant}-2x.png"
            output_path.write_bytes(artifact_response.content)
            artifact_record = {
                **artifact,
                "local_path": str(output_path.relative_to(ROOT)),
                "downloaded_bytes": len(artifact_response.content),
                "downloaded_sha256": _sha256(artifact_response.content),
            }

    manifest = {
        "schema_version": "dialecticore.production_v2.upscale_qualification.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "source": {
            "path": str(source_path.relative_to(ROOT)),
            "bytes": len(source),
            "sha256": _sha256(source),
            "upload": source_upload,
        },
        "request": {
            **payload,
            "input": {**payload["input"], "source_image": "<private upload reference>"},
        },
        "job": {
            key: job.get(key)
            for key in (
                "id",
                "state",
                "runtime",
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
        },
        "artifact": artifact_record,
        "wall_time_seconds": round(time.monotonic() - started, 3),
    }
    manifest_path = args.output / f"{args.participant}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(manifest_path.relative_to(ROOT))
    print(job_id, job.get("state"), artifact_record and artifact_record["local_path"])
    return 0 if job.get("state") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
