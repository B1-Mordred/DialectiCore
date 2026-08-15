from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from app.core.config import Settings
from app.services.model_gateway import SecretResolver

TERMINAL_STATES = {"completed", "failed", "cancelled", "expired", "recovery_required"}


class B1ManagedMediaSmokeService:
    def __init__(
        self,
        settings: Settings,
        *,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport

    async def run_smoke(
        self,
        *,
        api_base: str = "https://api.ai.b1.germering",
        model: str = "image-default",
        prompt: str = "small neutral studio lighting test card, no text",
        negative_prompt: str = "text, watermark, logo",
        width: int = 128,
        height: int = 128,
        steps: int = 1,
        cfg: float = 1.0,
        seed: int = 7,
        poll_attempts: int = 12,
        poll_interval_seconds: float = 10.0,
        evidence_output: str | None = None,
        requirements_output: str | None = "/home/mordred/media-requirements.md",
        allow_runner_failure: bool = False,
    ) -> dict[str, Any]:
        api_base = api_base.rstrip("/")
        result: dict[str, Any] = {
            "schema_version": "b1_managed_media_smoke_evidence.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "api_base": api_base,
            "model": model,
            "modality": _infer_modality(model),
            "operation": _infer_operation(model),
            "poll_attempts": poll_attempts,
            "poll_interval_seconds": poll_interval_seconds,
            "allow_runner_failure": allow_runner_failure,
        }
        try:
            token = self.secret_resolver.resolve("env:B1_API_KEY")
            ca_file = self._ca_file()
            payload = _media_job_payload(
                result,
                model=model,
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                cfg=cfg,
                seed=seed,
            )
            submit_route = _submit_route(result)
            result["submit_route"] = submit_route
            result["poll_route"] = "/v1/media/jobs/{job_id}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": _idempotency_key(payload),
            }
            async with httpx.AsyncClient(
                timeout=120,
                verify=str(ca_file),
                transport=self.transport,
            ) as client:
                submitted = await self._submit_media_job(
                    client,
                    api_base,
                    headers,
                    payload,
                    submit_route=submit_route,
                )
                result["submit"] = _submit_summary(submitted)
                job_id = _extract_job_id(submitted)
                if not job_id:
                    raise ValueError("B1 managed media response did not include a job id")
                result["job_id"] = job_id
                result["polls"] = await self._poll_media_job(
                    client,
                    api_base,
                    headers,
                    job_id,
                    attempts=max(1, poll_attempts),
                    interval_seconds=max(0.0, poll_interval_seconds),
                )
        except Exception as exc:
            if _is_scheduler_busy_error(exc):
                result["status"] = "busy"
                result["busy"] = _scheduler_busy_summary(exc)
                result["error"] = "B1 managed-media scheduler is busy; retry the smoke later"
                exit_code = 0
            else:
                result["status"] = "fail"
                result["error"] = f"{type(exc).__name__}: {exc}"
                exit_code = 1
            self._finalize_result(
                result,
                evidence_output=evidence_output,
                requirements_output=requirements_output,
            )
            return {"result": result, "exit_code": exit_code}

        terminal = _terminal_poll(result["polls"])
        result["terminal"] = terminal
        if terminal.get("state") == "completed":
            result["status"] = "pass"
            exit_code = 0
        elif terminal.get("state") in TERMINAL_STATES:
            result["status"] = "runner_failed"
            exit_code = 0 if allow_runner_failure else 2
        else:
            result["status"] = "timeout"
            exit_code = 2
        self._finalize_result(
            result,
            evidence_output=evidence_output,
            requirements_output=requirements_output,
        )
        return {"result": result, "exit_code": exit_code}

    def _ca_file(self) -> Path:
        ca_file = (
            Path(self.settings.runtime_state_path)
            / "certificates"
            / "b1-ai-hub-caddy-root.crt"
        )
        if not ca_file.is_file():
            raise ValueError(f"B1 CA file is not available: {ca_file}")
        return ca_file

    async def _submit_media_job(
        self,
        client: httpx.AsyncClient,
        api_base: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        submit_route: str,
    ) -> dict[str, Any]:
        response = await client.post(
            f"{api_base}{submit_route}",
            headers=headers,
            json=_submit_payload_for_route(payload, submit_route),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("B1 managed media response was not an object")
        return data

    async def _poll_media_job(
        self,
        client: httpx.AsyncClient,
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
            response = await client.get(
                f"{api_base}/v1/media/jobs/{job_id}",
                headers=poll_headers,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("B1 managed media status response was not an object")
            summary = _job_summary(data) | {"attempt": attempt}
            polls.append(summary)
            if summary.get("state") in TERMINAL_STATES:
                break
            if attempt < attempts and interval_seconds > 0:
                await asyncio.sleep(interval_seconds)
        return polls

    def _finalize_result(
        self,
        result: dict[str, Any],
        *,
        evidence_output: str | None,
        requirements_output: str | None,
    ) -> None:
        evidence = self._write_evidence(
            result,
            evidence_output or self.settings.b1_managed_media_smoke_evidence_path,
        )
        result["evidence_file"] = evidence
        if requirements_output and result.get("status") not in {"pass", "busy"}:
            result["requirements_update"] = _append_media_requirements(
                Path(requirements_output),
                result,
            )

    def _write_evidence(self, result: dict[str, Any], evidence_output: str) -> dict[str, Any]:
        path = Path(evidence_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(payload)
        return {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }


def _infer_modality(model: str) -> str:
    return "video" if model.startswith("video-") else "image"


def _infer_operation(model: str) -> str:
    if model == "image-upscale":
        return "upscaling"
    if model == "image-edit":
        return "image-edit"
    if model == "video-image":
        return "image-to-video"
    if model == "video-text":
        return "video-generation"
    return "image-generation"


def _media_job_payload(
    result: dict[str, Any],
    *,
    model: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
) -> dict[str, Any]:
    return {
        "modality": result["modality"],
        "operation": result["operation"],
        "model": model,
        "input": {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
        },
        "priority": "single_image",
        "runtime_policy": "comfyui",
    }


def _submit_route(result: dict[str, Any]) -> str:
    if (
        result.get("model") == "image-default"
        and result.get("modality") == "image"
        and result.get("operation") == "image-generation"
    ):
        return "/v1/images/generations"
    return "/v1/media/jobs"


def _submit_payload_for_route(payload: dict[str, Any], submit_route: str) -> dict[str, Any]:
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


def _idempotency_key(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"dialecticore-b1-media-smoke-{digest[:24]}"


def _extract_job_id(payload: dict[str, Any]) -> str | None:
    for key in ("id", "job_id", "b1_job_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _submit_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": _extract_job_id(payload),
        "state": payload.get("state") or payload.get("b1_status"),
        "stage": payload.get("stage"),
        "progress": payload.get("progress"),
        "model_alias": payload.get("model_alias") or payload.get("model"),
        "operation": payload.get("operation"),
        "modality": payload.get("modality"),
        "artifact_count": len(payload.get("artifacts") or []),
    }


def _job_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": _extract_job_id(payload),
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
    }


def _terminal_poll(polls: list[dict[str, Any]]) -> dict[str, Any]:
    return polls[-1] if polls else {"state": "missing"}


def _is_scheduler_busy_error(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409


def _scheduler_busy_summary(exc: Exception) -> dict[str, Any]:
    if not isinstance(exc, httpx.HTTPStatusError):
        return {}
    response = exc.response
    details: dict[str, Any] = {"status_code": response.status_code}
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        for key in ("reason", "detail", "message", "error", "retry_after_seconds"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)):
                details[key] = value
    return details


def _append_media_requirements(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else {}
    submit = result.get("submit") if isinstance(result.get("submit"), dict) else {}
    last_poll = terminal if terminal else submit
    job_id = result.get("job_id") or submit.get("job_id") or last_poll.get("job_id")
    added_at = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    section = "\n".join(
        [
            "",
            f"### B1 Managed Media Smoke Recheck Added {added_at}",
            "",
            "DialectiCore could not complete a B1 managed-media job needed for "
            "talkshow visual production.",
            "This note is written for Codex on the remote B1 server to diagnose "
            "and fix the B1 appliance side.",
            "",
            "- API surface: `POST /api/v1/system/b1-managed-media-smoke`",
            f"- B1 submit route: `POST {result.get('submit_route') or '/v1/media/jobs'}`",
            f"- B1 poll route: `GET {result.get('poll_route') or '/v1/media/jobs/{job_id}'}`",
            f"- API base: `{result.get('api_base')}`",
            f"- model alias: `{result.get('model')}`",
            f"- modality: `{result.get('modality')}`",
            f"- operation: `{result.get('operation')}`",
            f"- smoke status: `{result.get('status')}`",
            f"- job_id: `{job_id}`",
            f"- terminal state: `{last_poll.get('state')}`",
            f"- terminal stage: `{last_poll.get('stage')}`",
            f"- failure category: `{last_poll.get('failure_category')}`",
            f"- failure message: `{last_poll.get('failure_message')}`",
            f"- native ComfyUI prompt id: `{last_poll.get('native_prompt_id')}`",
            f"- artifact count: `{last_poll.get('artifact_count')}`",
            f"- error: `{result.get('error')}`",
            "",
            "Expected B1-side behavior for DialectiCore:",
            "",
            "- the selected B1 submit route accepts the selected model alias "
            "without a server error.",
            "- `GET /v1/media/jobs/{job_id}` eventually returns `state=completed` before timeout.",
            "- The terminal job payload includes at least one downloadable artifact "
            "for image/video presets.",
            "- DialectiCore `POST /api/v1/system/b1-managed-media-smoke` returns "
            "`result.status=pass`.",
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
