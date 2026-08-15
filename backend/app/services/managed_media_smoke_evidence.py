from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

MANAGED_MEDIA_SMOKE_FRESHNESS_SECONDS = 24 * 60 * 60


def managed_media_smoke_evidence(
    path: str | None,
    *,
    now: datetime | None = None,
) -> dict:
    configured_path = str(path or "").strip()
    if not configured_path:
        return {
            "schema_version": "managed_media_smoke_evidence_summary.v1",
            "configured": False,
            "status": "not_configured",
            "ready": None,
        }
    evidence_path = Path(configured_path)
    if not evidence_path.is_file():
        return {
            "schema_version": "managed_media_smoke_evidence_summary.v1",
            "configured": True,
            "path": configured_path,
            "status": "missing",
            "ready": False,
        }
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema_version": "managed_media_smoke_evidence_summary.v1",
            "configured": True,
            "path": configured_path,
            "status": "invalid",
            "ready": False,
            "error_type": type(exc).__name__,
        }
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if not isinstance(result, dict):
        return {
            "schema_version": "managed_media_smoke_evidence_summary.v1",
            "configured": True,
            "path": configured_path,
            "status": "invalid",
            "ready": False,
            "error_type": "UnexpectedPayload",
        }
    terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else {}
    submit = result.get("submit") if isinstance(result.get("submit"), dict) else {}
    status = str(result.get("status") or "")
    created_at = result.get("created_at")
    age_seconds = _evidence_age_seconds(created_at, now=now)
    fresh = (
        age_seconds <= MANAGED_MEDIA_SMOKE_FRESHNESS_SECONDS
        if age_seconds is not None
        else None
    )
    return {
        "schema_version": "managed_media_smoke_evidence_summary.v1",
        "configured": True,
        "path": configured_path,
        "status": status or "unknown",
        "ready": status == "pass",
        "created_at": created_at,
        "age_seconds": age_seconds,
        "fresh": fresh,
        "freshness_window_seconds": MANAGED_MEDIA_SMOKE_FRESHNESS_SECONDS,
        "api_base": result.get("api_base"),
        "model": result.get("model"),
        "modality": result.get("modality"),
        "operation": result.get("operation"),
        "job_id": result.get("job_id") or submit.get("job_id") or terminal.get("job_id"),
        "terminal_state": terminal.get("state"),
        "terminal_stage": terminal.get("stage"),
        "failure_category": terminal.get("failure_category"),
        "failure_message": terminal.get("failure_message"),
        "artifact_count": terminal.get("artifact_count"),
        "busy": status == "busy",
        "busy_details": result.get("busy") if status == "busy" else None,
        "action": managed_media_smoke_operator_action(status, fresh=fresh),
    }


def managed_media_smoke_operator_action(status: str, *, fresh: bool | None = None) -> str:
    if status == "pass":
        return "managed_media_smoke_ready" if fresh else "run_b1_managed_media_smoke"
    if status == "busy":
        return "wait_for_b1_media_capacity_then_rerun_smoke"
    if status in {"runner_failed", "fail", "timeout"}:
        if fresh is False:
            return "run_b1_managed_media_smoke"
        return "fix_b1_managed_media_runner_then_rerun_smoke"
    if status == "missing":
        return "run_b1_managed_media_smoke"
    return "inspect_b1_managed_media_smoke"


def _evidence_age_seconds(created_at: object, *, now: datetime | None) -> int | None:
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0, int((reference - parsed).total_seconds()))
