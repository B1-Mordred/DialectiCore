from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.core.config import Settings
from app.domain.schemas import (
    WorkerHeartbeatRequest,
    WorkerLeaseRecord,
    WorkerStatusRecord,
    WorkerStatusSummary,
)

WORKER_ROLES = [
    "workflow-worker",
    "temporal-worker",
    "discussion-worker",
    "research-worker",
    "localization-worker",
    "voicebox-adapter",
    "comfyui-adapter",
    "timeline-worker",
    "render-worker",
    "qc-worker",
    "publishing-worker",
]


def configured_worker_roles(settings: Settings) -> list[str]:
    configured = [
        role.strip() for role in settings.worker_required_roles.split(",") if role.strip()
    ]
    unknown = [role for role in configured if role not in WORKER_ROLES]
    if unknown:
        raise ValueError(f"unsupported configured worker roles: {', '.join(unknown)}")
    roles = configured or ["workflow-worker"]
    temporal_enabled = (
        settings.temporal_backend_mode.strip().lower() == "external"
        and settings.temporal_backend_worker_enabled
    )
    if temporal_enabled and "temporal-worker" not in roles:
        roles.append("temporal-worker")
    if not temporal_enabled:
        roles = [role for role in roles if role != "temporal-worker"]
    return roles


class WorkerStatusService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.runtime_state_path).expanduser() / "workers"

    def record_heartbeat(self, request: WorkerHeartbeatRequest) -> WorkerStatusRecord:
        now = datetime.now(UTC)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(request.role, request.worker_id)
        first_seen_at = now
        if path.exists():
            try:
                first_seen_at = WorkerStatusRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                ).first_seen_at
            except (OSError, ValueError):
                first_seen_at = now

        record = WorkerStatusRecord(
            role=request.role,
            worker_id=request.worker_id,
            status=request.status,
            details=request.details,
            first_seen_at=first_seen_at,
            last_heartbeat_at=now,
            heartbeat_age_seconds=0,
            stale=False,
        )
        self._write_record(path, record)
        return record

    def list_workers(self) -> list[WorkerStatusRecord]:
        workers, _ = self._list_workers_with_cleanup()
        return workers

    def summary(
        self,
        leases: list[WorkerLeaseRecord] | None = None,
        lease_cleanup_counts: dict[str, int] | None = None,
    ) -> WorkerStatusSummary:
        workers, pruned_heartbeat_count = self._list_workers_with_cleanup()
        leases = leases or []
        cleanup_counts = self.cleanup_malformed_heartbeats() | (lease_cleanup_counts or {})
        active_lease_count = sum(1 for lease in leases if not lease.expired)
        expired_lease_count = sum(1 for lease in leases if lease.expired)
        malformed_count = int(cleanup_counts.get("malformed_heartbeats") or 0) + int(
            cleanup_counts.get("malformed_leases") or 0
        )
        configured_roles = configured_worker_roles(self.settings)
        current_role_workers = {
            role: [worker for worker in workers if worker.role == role and not worker.stale]
            for role in configured_roles
        }
        stale_count = sum(
            1
            for role in configured_roles
            if not current_role_workers[role]
            and any(worker.role == role and worker.stale for worker in workers)
        )
        failed_count = sum(
            1
            for role, role_workers in current_role_workers.items()
            if role_workers
            and any(worker.status == "failed" for worker in role_workers)
            and not any(worker.status != "failed" for worker in role_workers)
        )
        degraded_count = sum(
            1
            for role, role_workers in current_role_workers.items()
            if role_workers
            and any(worker.status == "degraded" for worker in role_workers)
            and not any(worker.status not in {"failed", "degraded"} for worker in role_workers)
        )
        # `active_workers` is an inventory count, not merely a count of the
        # roles required by this particular deployment.  Keep configured-role
        # coverage separate in `active_roles`/`all_configured_roles_active`.
        active_count = sum(
            1
            for worker in workers
            if not worker.stale
            and worker.status in {"running", "idle"}
        )
        expected_roles_present = {
            role
            for role in configured_roles
            if any(worker.role == role and not worker.stale for worker in workers)
        }
        all_configured_roles_active = len(expected_roles_present) >= len(configured_roles)
        if failed_count:
            status = "unhealthy"
        elif (
            stale_count
            or degraded_count
            or malformed_count
            or active_count == 0
            or not all_configured_roles_active
        ):
            status = "degraded"
        else:
            status = "healthy"
        return WorkerStatusSummary(
            status=status,
            heartbeat_ttl_seconds=self.settings.worker_heartbeat_ttl_seconds,
            lease_ttl_seconds=self.settings.worker_lease_ttl_seconds,
            runtime_state_retention_seconds=self._retention_seconds(),
            workers=workers,
            leases=leases,
            counts={
                "configured_roles": len(configured_roles),
                "active_workers": active_count,
                "stale_workers": stale_count,
                "failed_workers": failed_count,
                "degraded_workers": degraded_count,
                "active_roles": len(expected_roles_present),
                "active_leases": active_lease_count,
                "expired_leases": expired_lease_count,
                "pruned_expired_leases": cleanup_counts.get("pruned_expired_leases", 0),
                "retained_heartbeats": len(workers),
                "pruned_stale_heartbeats": pruned_heartbeat_count,
                "malformed_heartbeats": cleanup_counts.get("malformed_heartbeats", 0),
                "pruned_malformed_heartbeats": cleanup_counts.get(
                    "pruned_malformed_heartbeats",
                    0,
                ),
                "malformed_leases": cleanup_counts.get("malformed_leases", 0),
                "pruned_malformed_leases": cleanup_counts.get(
                    "pruned_malformed_leases",
                    0,
                ),
            },
        )

    def _list_workers_with_cleanup(self) -> tuple[list[WorkerStatusRecord], int]:
        now = datetime.now(UTC)
        records, pruned_count = self._read_records(now)
        workers = [self._refresh_record(record, now) for record in records]
        return sorted(workers, key=lambda item: (item.role, item.worker_id)), pruned_count

    def _read_records(self, now: datetime) -> tuple[list[WorkerStatusRecord], int]:
        if not self.root.exists():
            return [], 0
        records: list[WorkerStatusRecord] = []
        pruned_count = 0
        for path in sorted(self.root.glob("*.json")):
            if not self._looks_like_heartbeat_path(path):
                continue
            try:
                record = WorkerStatusRecord.model_validate_json(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if self._should_prune_record(record, now):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    records.append(record)
                    continue
                pruned_count += 1
                continue
            records.append(record)
        return records, pruned_count

    def cleanup_malformed_heartbeats(self) -> dict[str, int]:
        if not self.root.exists():
            return {"malformed_heartbeats": 0, "pruned_malformed_heartbeats": 0}
        malformed_count = 0
        pruned_count = 0
        for path in sorted(self.root.glob("*.json")):
            if not self._looks_like_heartbeat_path(path):
                continue
            try:
                WorkerStatusRecord.model_validate_json(path.read_text("utf-8"))
            except (OSError, ValueError):
                malformed_count += 1
                if self._malformed_file_age_seconds(path) > self._retention_seconds():
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue
                    pruned_count += 1
        return {
            "malformed_heartbeats": malformed_count,
            "pruned_malformed_heartbeats": pruned_count,
        }

    def _refresh_record(
        self,
        record: WorkerStatusRecord,
        now: datetime,
    ) -> WorkerStatusRecord:
        age = max(0.0, (now - record.last_heartbeat_at).total_seconds())
        return record.model_copy(
            update={
                "heartbeat_age_seconds": age,
                "stale": age > self.settings.worker_heartbeat_ttl_seconds,
            }
        )

    def _should_prune_record(self, record: WorkerStatusRecord, now: datetime) -> bool:
        age = max(0.0, (now - record.last_heartbeat_at).total_seconds())
        return age > self._retention_seconds()

    def _retention_seconds(self) -> int:
        return max(
            self.settings.worker_runtime_state_retention_seconds,
            self.settings.worker_heartbeat_ttl_seconds,
        )

    def _path_for(self, role: str, worker_id: str) -> Path:
        return self.root / f"{self._slug(role)}--{self._slug(worker_id)}.json"

    def _looks_like_heartbeat_path(self, path: Path) -> bool:
        return "--" in path.stem

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
        return slug[:160] or "worker"

    def _malformed_file_age_seconds(self, path: Path) -> float:
        try:
            return max(0.0, datetime.now(UTC).timestamp() - path.stat().st_mtime)
        except OSError:
            return self._retention_seconds() + 1

    def _write_record(self, path: Path, record: WorkerStatusRecord) -> None:
        payload = record.model_dump_json(indent=2)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.write("\n")
            temp_name = handle.name
        os.replace(temp_name, path)
