from __future__ import annotations

import os
import socket
import sys

from app.core.config import Settings
from app.services.worker_status_service import WORKER_ROLES, WorkerStatusService


def worker_health_status(
    settings: Settings,
    role: str,
    hostname: str | None = None,
) -> dict:
    if role not in WORKER_ROLES:
        raise RuntimeError(f"DIALECTICORE_WORKER_ROLE is not supported: {role}")
    container_host = hostname or socket.gethostname()
    workers = [
        worker
        for worker in WorkerStatusService(settings).list_workers()
        if worker.role == role and worker.worker_id.startswith(f"{container_host}:")
    ]
    if not workers:
        raise RuntimeError(f"no heartbeat found for {role} on {container_host}")
    latest = max(workers, key=lambda worker: worker.last_heartbeat_at)
    if latest.stale:
        raise RuntimeError(
            f"latest heartbeat for {role} on {container_host} is stale "
            f"({latest.heartbeat_age_seconds:.1f}s old)"
        )
    if latest.status in {"failed", "degraded"}:
        raise RuntimeError(f"latest heartbeat for {role} reports {latest.status}")
    return {
        "role": latest.role,
        "worker_id": latest.worker_id,
        "status": latest.status,
        "heartbeat_age_seconds": latest.heartbeat_age_seconds,
    }


def main() -> int:
    role = os.getenv("DIALECTICORE_WORKER_ROLE", "workflow-worker")
    settings = Settings()
    try:
        worker_health_status(settings, role)
    except RuntimeError as exc:
        print(f"worker healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
