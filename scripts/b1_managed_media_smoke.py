#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any

import httpx

DEFAULT_API_BASE = "https://api.ai.b1.germering"
DEFAULT_CA_FILE = "storage/runtime-state/certificates/b1-ai-hub-caddy-root.crt"
DEFAULT_EVIDENCE_OUTPUT = "output/smoke/b1-managed-media-smoke-evidence.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Submit and poll a tiny B1 managed media job with stable evidence."
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--ca-file", default=DEFAULT_CA_FILE)
    parser.add_argument("--api-key-env", default="B1_API_KEY")
    parser.add_argument("--model", default="image-default")
    parser.add_argument("--modality", default="")
    parser.add_argument("--operation", default="")
    parser.add_argument(
        "--prompt",
        default="small neutral studio lighting test card, no text",
    )
    parser.add_argument("--negative-prompt", default="text, watermark, logo")
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=128)
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--cfg", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--poll-attempts", type=int, default=12)
    parser.add_argument("--poll-interval-seconds", type=float, default=10.0)
    parser.add_argument(
        "--allow-runner-failure",
        action="store_true",
        help="Exit 0 when B1 accepts the job but the remote runner fails terminally.",
    )
    parser.add_argument(
        "--evidence-output",
        default=DEFAULT_EVIDENCE_OUTPUT,
        help="Where to write stable JSON evidence.",
    )
    parser.add_argument(
        "--requirements-output",
        default="",
        help=(
            "Optional markdown file to append B1 managed-media failure requirements to "
            "when media creation does not complete."
        ),
    )
    parser.add_argument("--no-evidence-file", action="store_true")
    args = parser.parse_args()

    load_env_file(Path(args.env_file))
    api_base = args.api_base.rstrip("/")
    result: dict[str, Any] = {
        "schema_version": "b1_managed_media_smoke_evidence.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "api_base": api_base,
        "model": args.model,
        "modality": args.modality or infer_modality(args.model),
        "operation": args.operation or infer_operation(args.model),
        "poll_attempts": args.poll_attempts,
        "poll_interval_seconds": args.poll_interval_seconds,
        "allow_runner_failure": args.allow_runner_failure,
    }
    try:
        token = os.environ.get(args.api_key_env, "")
        if not token:
            raise ValueError(f"{args.api_key_env} is not set")
        ca_file = Path(args.ca_file)
        if not ca_file.is_file():
            raise ValueError(f"B1 CA file is not available: {ca_file}")
        payload = media_job_payload(args, result)
        submit_route = submit_route_for_result(result)
        result["submit_route"] = submit_route
        result["poll_route"] = "/v1/media/jobs/{job_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key(payload),
        }
        with httpx.Client(timeout=120, verify=str(ca_file)) as client:
            submitted = submit_media_job(
                client,
                api_base,
                headers,
                payload,
                submit_route=submit_route,
            )
            result["submit"] = submit_summary(submitted)
            job_id = extract_job_id(submitted)
            if not job_id:
                raise ValueError("B1 managed media response did not include a job id")
            result["job_id"] = job_id
            result["polls"] = poll_media_job(
                client,
                api_base,
                headers,
                job_id,
                attempts=max(1, args.poll_attempts),
                interval_seconds=max(0.0, args.poll_interval_seconds),
            )
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        maybe_append_media_requirements(args, result)
        emit_result(result, args)
        return 1

    terminal = terminal_poll(result["polls"])
    result["terminal"] = terminal
    if terminal.get("state") == "completed":
        result["status"] = "pass"
        exit_code = 0
    elif terminal.get("state") in TERMINAL_STATES:
        result["status"] = "runner_failed"
        exit_code = 0 if args.allow_runner_failure else 2
    else:
        result["status"] = "timeout"
        exit_code = 2
    maybe_append_media_requirements(args, result)
    emit_result(result, args)
    return exit_code


TERMINAL_STATES = {"completed", "failed", "cancelled", "expired", "recovery_required"}


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def infer_modality(model: str) -> str:
    return "video" if model.startswith("video-") else "image"


def infer_operation(model: str) -> str:
    if model == "image-upscale":
        return "upscaling"
    if model == "image-edit":
        return "image-edit"
    if model == "video-image":
        return "image-to-video"
    if model == "video-text":
        return "video-generation"
    return "image-generation"


def media_job_payload(args: argparse.Namespace, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "modality": result["modality"],
        "operation": result["operation"],
        "model": args.model,
        "input": {
            "prompt": args.prompt,
            "negative_prompt": args.negative_prompt,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "cfg": args.cfg,
            "seed": args.seed,
        },
        "priority": "single_image",
        "runtime_policy": "comfyui",
    }


def submit_route_for_result(result: dict[str, Any]) -> str:
    if (
        result.get("model") == "image-default"
        and result.get("modality") == "image"
        and result.get("operation") == "image-generation"
    ):
        return "/v1/images/generations"
    return "/v1/media/jobs"


def submit_payload_for_route(payload: dict[str, Any], submit_route: str) -> dict[str, Any]:
    if submit_route != "/v1/images/generations":
        return payload
    media_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    width = int(media_input.get("width") or 128)
    height = int(media_input.get("height") or 128)
    return {
        "model": payload.get("model"),
        "prompt": media_input.get("prompt") or "",
        "n": 1,
        "size": f"{width}x{height}",
        "negative_prompt": media_input.get("negative_prompt"),
        "steps": media_input.get("steps"),
        "cfg": media_input.get("cfg"),
        "seed": media_input.get("seed"),
    }


def idempotency_key(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"dialecticore-b1-media-smoke-{digest[:24]}"


def submit_media_job(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    submit_route: str,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}{submit_route}",
        headers=headers,
        json=submit_payload_for_route(payload, submit_route),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("B1 managed media response was not an object")
    return data


def poll_media_job(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    job_id: str,
    *,
    attempts: int,
    interval_seconds: float,
) -> list[dict[str, Any]]:
    polls = []
    poll_headers = {
        "Authorization": headers["Authorization"],
        "Accept": "application/json",
    }
    for attempt in range(1, attempts + 1):
        response = client.get(f"{api_base}/v1/media/jobs/{job_id}", headers=poll_headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("B1 managed media status response was not an object")
        summary = job_summary(data) | {"attempt": attempt}
        polls.append(summary)
        if summary.get("state") in TERMINAL_STATES:
            break
        if attempt < attempts and interval_seconds > 0:
            sleep(interval_seconds)
    return polls


def extract_job_id(payload: dict[str, Any]) -> str | None:
    for key in ("id", "job_id", "b1_job_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def submit_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": extract_job_id(payload),
        "state": payload.get("state") or payload.get("b1_status"),
        "stage": payload.get("stage"),
        "progress": payload.get("progress"),
        "model_alias": payload.get("model_alias") or payload.get("model"),
        "operation": payload.get("operation"),
        "modality": payload.get("modality"),
        "artifact_count": len(payload.get("artifacts") or []),
        "links": payload.get("links")
        or {
            key: payload.get(key)
            for key in ("b1_job_url", "b1_events_url", "b1_artifacts_url", "b1_cancel_url")
            if payload.get(key)
        },
    }


def job_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": extract_job_id(payload),
        "state": payload.get("state") or payload.get("b1_status"),
        "stage": payload.get("stage"),
        "progress": payload.get("progress"),
        "artifact_count": len(payload.get("artifacts") or []),
        "failure_category": payload.get("failure_category"),
        "failure_message": payload.get("failure_message"),
        "runtime": payload.get("runtime"),
        "model_alias": payload.get("model_alias"),
        "operation": payload.get("operation"),
        "modality": payload.get("modality"),
        "native_prompt_id": payload.get("native_prompt_id"),
        "load_time_ms": payload.get("load_time_ms"),
        "run_time_ms": payload.get("run_time_ms"),
        "peak_vram_mib": payload.get("peak_vram_mib"),
        "peak_ram_mib": payload.get("peak_ram_mib"),
        "artifacts": artifact_summaries(payload.get("artifacts")),
    }


def artifact_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "id": artifact.get("id"),
            "kind": artifact.get("kind"),
            "mime_type": artifact.get("mime_type"),
            "bytes": artifact.get("bytes"),
            "sha256": artifact.get("sha256"),
            "url": artifact.get("url"),
            "path": artifact.get("path"),
            "ingest_status": artifact.get("ingest_status"),
        }
        for artifact in value
        if isinstance(artifact, dict)
    ]


def terminal_poll(polls: list[dict[str, Any]]) -> dict[str, Any]:
    return polls[-1] if polls else {"state": "missing"}


def emit_result(result: dict[str, Any], args: argparse.Namespace) -> None:
    output = {"result": result}
    if not args.no_evidence_file:
        path = Path(args.evidence_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(payload)
        output["evidence_file"] = {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    print(json.dumps(output, indent=2, sort_keys=True))


def maybe_append_media_requirements(
    args: argparse.Namespace,
    result: dict[str, Any],
) -> None:
    if not args.requirements_output:
        return
    if result.get("status") == "pass":
        return
    result["requirements_update"] = append_media_requirements(
        Path(args.requirements_output),
        result,
    )


def append_media_requirements(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    submit = result.get("submit") if isinstance(result.get("submit"), dict) else {}
    terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else {}
    polls = result.get("polls") if isinstance(result.get("polls"), list) else []
    last_poll = (
        terminal
        if terminal
        else (polls[-1] if polls and isinstance(polls[-1], dict) else {})
    )
    job_id = result.get("job_id") or submit.get("job_id") or last_poll.get("job_id")
    added_at = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    section = "\n".join(
        [
            "",
            f"### B1 Managed Media Smoke Recheck Added {added_at}",
            "",
            (
                "DialectiCore could not complete a B1 managed-media job needed "
                "for talkshow visual production."
            ),
            (
                "This note is written for Codex on the remote B1 server to "
                "diagnose and fix the B1 appliance side."
            ),
            "",
            "- script: `scripts/b1_managed_media_smoke.py`",
            f"- API base: `{result.get('api_base')}`",
            f"- submit route: `POST {result.get('submit_route') or '/v1/media/jobs'}`",
            f"- poll route: `GET {result.get('poll_route') or '/v1/media/jobs/{job_id}'}`",
            f"- model alias: `{result.get('model')}`",
            f"- modality: `{result.get('modality')}`",
            f"- operation: `{result.get('operation')}`",
            f"- requested poll attempts: `{result.get('poll_attempts')}`",
            f"- requested poll interval seconds: `{result.get('poll_interval_seconds')}`",
            f"- smoke status: `{result.get('status')}`",
            f"- job_id: `{job_id}`",
            f"- submit state: `{submit.get('state')}`",
            f"- terminal state: `{last_poll.get('state')}`",
            f"- terminal stage: `{last_poll.get('stage')}`",
            f"- terminal progress: `{last_poll.get('progress')}`",
            f"- failure category: `{last_poll.get('failure_category')}`",
            f"- failure message: `{last_poll.get('failure_message')}`",
            f"- native ComfyUI prompt id: `{last_poll.get('native_prompt_id')}`",
            f"- artifact count: `{last_poll.get('artifact_count')}`",
            f"- error: `{result.get('error')}`",
            "",
            "Expected B1-side behavior for DialectiCore:",
            "",
            (
                "- the selected B1 submit route accepts the request for the "
                "selected model alias without a server error."
            ),
            (
                "- `GET /v1/media/jobs/{job_id}` eventually returns "
                "`state=completed` before the smoke timeout."
            ),
            (
                "- The terminal job payload includes at least one downloadable "
                "artifact for image/video generation presets."
            ),
            (
                "- The failure fields are empty on success, or otherwise identify "
                "a concrete runner/runtime issue."
            ),
            (
                "- DialectiCore `scripts/b1_managed_media_smoke.py` exits 0 "
                "without `--allow-runner-failure`."
            ),
            "",
            "Relevant presets DialectiCore expects from B1 for talkshow production:",
            "",
            "- `image-default`: SD 1.5 text-to-image",
            "- `image-edit`: SD 1.5 image edit/inpaint workflows",
            "- `image-upscale`: Real-ESRGAN x4plus",
            "- `video-text`: Wan 2.1 T2V 1.3B",
            "- `video-image`: Wan 2.1 VACE 1.3B image/video-conditioned workflows",
            "",
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(section)
    return {"path": str(path), "appended": True}


if __name__ == "__main__":
    sys.exit(main())
