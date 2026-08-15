from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.core.config import Settings
from app.domain.schemas import WorkerLeaseRecord


class WorkerLeaseService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.runtime_state_path).expanduser() / "worker-leases"
        self.last_cleanup_counts = {
            "pruned_expired_leases": 0,
            "malformed_leases": 0,
            "pruned_malformed_leases": 0,
        }

    def acquire(self, role: str, worker_id: str) -> WorkerLeaseRecord | None:
        now = datetime.now(UTC)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._role_lock(role):
            existing = self._read_lease(role)
            if (
                existing is not None
                and existing.worker_id != worker_id
                and existing.expires_at > now
            ):
                return None

            acquired_at = now
            if existing is not None and existing.worker_id == worker_id:
                acquired_at = existing.acquired_at
            lease = WorkerLeaseRecord(
                role=role,
                worker_id=worker_id,
                acquired_at=acquired_at,
                last_renewed_at=now,
                expires_at=now + timedelta(seconds=self.settings.worker_lease_ttl_seconds),
            )
            self._write_lease(self._path_for(role), lease)
            return self._refresh_lease(lease, now)

    def renew(self, role: str, worker_id: str) -> WorkerLeaseRecord | None:
        return self.acquire(role, worker_id)

    def release(self, role: str, worker_id: str) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._role_lock(role):
            existing = self._read_lease(role)
            if existing is None or existing.worker_id != worker_id:
                return False
            self._path_for(role).unlink(missing_ok=True)
            return True

    def list_leases(self) -> list[WorkerLeaseRecord]:
        if not self.root.exists():
            self.last_cleanup_counts = {
                "pruned_expired_leases": 0,
                "malformed_leases": 0,
                "pruned_malformed_leases": 0,
            }
            return []
        now = datetime.now(UTC)
        leases: list[WorkerLeaseRecord] = []
        pruned_expired_count = 0
        malformed_count = 0
        pruned_malformed_count = 0
        for path in sorted(self.root.glob("*.json")):
            try:
                lease = WorkerLeaseRecord.model_validate_json(path.read_text("utf-8"))
            except (OSError, ValueError):
                malformed_count += 1
                if self._malformed_file_age_seconds(path) > self._retention_seconds():
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue
                    pruned_malformed_count += 1
                continue
            if self._should_prune_lease(lease, now):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    leases.append(self._refresh_lease(lease, now))
                    continue
                pruned_expired_count += 1
                continue
            leases.append(self._refresh_lease(lease, now))
        self.last_cleanup_counts = {
            "pruned_expired_leases": pruned_expired_count,
            "malformed_leases": malformed_count,
            "pruned_malformed_leases": pruned_malformed_count,
        }
        return sorted(leases, key=lambda item: item.role)

    def _read_lease(self, role: str) -> WorkerLeaseRecord | None:
        path = self._path_for(role)
        if not path.exists():
            return None
        try:
            return WorkerLeaseRecord.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def _refresh_lease(
        self,
        lease: WorkerLeaseRecord,
        now: datetime,
    ) -> WorkerLeaseRecord:
        return lease.model_copy(
            update={
                "lease_age_seconds": max(0.0, (now - lease.acquired_at).total_seconds()),
                "expires_in_seconds": (lease.expires_at - now).total_seconds(),
                "expired": lease.expires_at <= now,
            }
        )

    def _should_prune_lease(self, lease: WorkerLeaseRecord, now: datetime) -> bool:
        if lease.expires_at > now:
            return False
        expired_age = max(0.0, (now - lease.expires_at).total_seconds())
        return expired_age > self._retention_seconds()

    def _retention_seconds(self) -> int:
        return max(
            self.settings.worker_runtime_state_retention_seconds,
            self.settings.worker_lease_ttl_seconds,
        )

    def _path_for(self, role: str) -> Path:
        return self.root / f"{self._slug(role)}.json"

    def _lock_path_for(self, role: str) -> Path:
        return self.root / f"{self._slug(role)}.lock"

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
        return slug[:160] or "worker"

    @contextmanager
    def _role_lock(self, role: str) -> Iterator[None]:
        lock_path = self._lock_path_for(role)
        deadline = time.monotonic() + 5
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if self._lock_is_stale(lock_path):
                    lock_path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring worker lease lock for {role}"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def _lock_is_stale(self, path: Path) -> bool:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return True
        return age > max(5, self.settings.worker_lease_ttl_seconds)

    def _malformed_file_age_seconds(self, path: Path) -> float:
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return self._retention_seconds() + 1

    def _write_lease(self, path: Path, lease: WorkerLeaseRecord) -> None:
        payload = lease.model_dump_json(indent=2)
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
