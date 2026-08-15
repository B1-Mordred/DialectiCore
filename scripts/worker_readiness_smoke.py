#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_EVIDENCE_OUTPUT = "output/smoke/worker-readiness-smoke-evidence.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect DialectiCore worker heartbeat readiness without faking workers."
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--evidence-output", default=DEFAULT_EVIDENCE_OUTPUT)
    parser.add_argument("--no-evidence-file", action="store_true")
    parser.add_argument(
        "--require-all-roles",
        action="store_true",
        help="Return non-zero unless every configured worker role has a fresh heartbeat.",
    )
    args = parser.parse_args()

    api_base = args.api_base.rstrip("/")
    result: dict[str, Any] = {
        "schema_version": "worker_readiness_smoke_evidence.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "api_base": api_base,
        "require_all_roles": args.require_all_roles,
    }
    try:
        with httpx.Client(timeout=60) as client:
            workers = get_json(client, f"{api_base}/api/v1/system/workers")
            readiness = get_json(client, f"{api_base}/api/v1/system/live-provider-readiness")
            result["worker_summary"] = worker_summary(workers)
            result["worker_readiness"] = worker_readiness_summary(readiness)
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        emit_result(result, args)
        return 1

    failed_sections = []
    summary = result["worker_summary"]
    readiness_summary = result["worker_readiness"]
    if summary["failed_workers"] or summary["degraded_workers"]:
        failed_sections.append("failed_or_degraded_workers")
    if summary["malformed_heartbeats"] or summary["malformed_leases"]:
        failed_sections.append("malformed_worker_state")
    if args.require_all_roles and summary["missing_role_count"]:
        failed_sections.append("missing_worker_roles")
    if args.require_all_roles and readiness_summary.get("status") != "pass":
        failed_sections.append("worker_registry_readiness")
    result["status"] = "fail" if failed_sections else "pass"
    result["failed_sections"] = failed_sections
    emit_result(result, args)
    return 1 if failed_sections else 0


def get_json(client: httpx.Client, url: str) -> Any:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def worker_summary(payload: dict[str, Any]) -> dict[str, Any]:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    workers = payload.get("workers") if isinstance(payload.get("workers"), list) else []
    active = [
        worker
        for worker in workers
        if isinstance(worker, dict)
        and worker.get("stale") is not True
        and worker.get("status") in {"running", "idle"}
    ]
    return {
        "status": payload.get("status"),
        "configured_roles": counts.get("configured_roles", 0),
        "active_workers": counts.get("active_workers", 0),
        "active_roles": counts.get("active_roles", 0),
        "missing_role_count": max(
            0,
            int(counts.get("configured_roles") or 0) - int(counts.get("active_roles") or 0),
        ),
        "stale_workers": counts.get("stale_workers", 0),
        "failed_workers": counts.get("failed_workers", 0),
        "degraded_workers": counts.get("degraded_workers", 0),
        "malformed_heartbeats": counts.get("malformed_heartbeats", 0),
        "malformed_leases": counts.get("malformed_leases", 0),
        "heartbeat_ttl_seconds": payload.get("heartbeat_ttl_seconds"),
        "active_worker_ids": [
            {
                "role": worker.get("role"),
                "worker_id": worker.get("worker_id"),
                "status": worker.get("status"),
                "heartbeat_age_seconds": worker.get("heartbeat_age_seconds"),
            }
            for worker in active[:20]
        ],
    }


def worker_readiness_summary(readiness: dict[str, Any]) -> dict[str, Any]:
    worker = next(
        (
            check
            for check in readiness.get("checks", [])
            if isinstance(check, dict) and check.get("category") == "worker_registry"
        ),
        {},
    )
    details = worker.get("details") if isinstance(worker.get("details"), dict) else {}
    return {
        "status": worker.get("status"),
        "warnings": worker.get("warnings", []),
        "blockers": worker.get("blockers", []),
        "failed_readiness_checks": details.get("failed_readiness_checks", []),
        "missing_role_names": details.get("missing_role_names", []),
        "reason": details.get("reason"),
    }


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


if __name__ == "__main__":
    sys.exit(main())
