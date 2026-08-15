import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import Settings
from app.services.worker_lease_service import WorkerLeaseService


def test_worker_lease_service_acquires_renews_and_releases_role_lease(tmp_path: Path) -> None:
    service = WorkerLeaseService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_lease_ttl_seconds=30,
        )
    )

    acquired = service.acquire("voicebox-adapter", "worker-1")
    competing = service.acquire("voicebox-adapter", "worker-2")
    renewed = service.renew("voicebox-adapter", "worker-1")
    leases = service.list_leases()
    released = service.release("voicebox-adapter", "worker-1")

    assert acquired is not None
    assert acquired.worker_id == "worker-1"
    assert competing is None
    assert renewed is not None
    assert renewed.acquired_at == acquired.acquired_at
    assert len(leases) == 1
    assert leases[0].expired is False
    assert leases[0].expires_in_seconds > 0
    assert released is True
    assert service.list_leases() == []


def test_worker_lease_service_allows_stale_lease_takeover(tmp_path: Path) -> None:
    service = WorkerLeaseService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_lease_ttl_seconds=1,
        )
    )
    old = service.acquire("comfyui-adapter", "worker-1")
    assert old is not None
    stale = old.model_copy(
        update={
            "last_renewed_at": datetime.now(UTC) - timedelta(seconds=10),
            "expires_at": datetime.now(UTC) - timedelta(seconds=5),
        }
    )
    service._path_for("comfyui-adapter").write_text(
        stale.model_dump_json(),
        encoding="utf-8",
    )

    takeover = service.acquire("comfyui-adapter", "worker-2")

    assert takeover is not None
    assert takeover.worker_id == "worker-2"
    assert takeover.expired is False


def test_worker_lease_service_prunes_long_expired_leases(tmp_path: Path) -> None:
    service = WorkerLeaseService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_lease_ttl_seconds=1,
            worker_runtime_state_retention_seconds=2,
        )
    )
    lease = service.acquire("comfyui-adapter", "worker-1")
    assert lease is not None
    expired = lease.model_copy(
        update={
            "last_renewed_at": datetime.now(UTC) - timedelta(seconds=20),
            "expires_at": datetime.now(UTC) - timedelta(seconds=10),
        }
    )
    lease_path = service._path_for("comfyui-adapter")
    lease_path.write_text(expired.model_dump_json(), encoding="utf-8")

    leases = service.list_leases()

    assert leases == []
    assert service.last_cleanup_counts["pruned_expired_leases"] == 1
    assert lease_path.exists() is False


def test_worker_lease_service_reports_expired_leases_until_retention(
    tmp_path: Path,
) -> None:
    service = WorkerLeaseService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_lease_ttl_seconds=1,
            worker_runtime_state_retention_seconds=60,
        )
    )
    lease = service.acquire("voicebox-adapter", "worker-1")
    assert lease is not None
    expired = lease.model_copy(
        update={
            "last_renewed_at": datetime.now(UTC) - timedelta(seconds=10),
            "expires_at": datetime.now(UTC) - timedelta(seconds=5),
        }
    )
    lease_path = service._path_for("voicebox-adapter")
    lease_path.write_text(expired.model_dump_json(), encoding="utf-8")

    leases = service.list_leases()

    assert len(leases) == 1
    assert leases[0].expired is True
    assert leases[0].expires_in_seconds < 0
    assert service.last_cleanup_counts["pruned_expired_leases"] == 0
    assert lease_path.exists() is True


def test_worker_lease_service_reports_and_prunes_expired_malformed_leases(
    tmp_path: Path,
) -> None:
    service = WorkerLeaseService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_lease_ttl_seconds=1,
            worker_runtime_state_retention_seconds=2,
        )
    )
    service.root.mkdir(parents=True)
    fresh_malformed = service.root / "fresh-role.json"
    expired_malformed = service.root / "expired-role.json"
    fresh_malformed.write_text("{not-json", encoding="utf-8")
    expired_malformed.write_text("{not-json", encoding="utf-8")
    old_timestamp = time.time() - 10
    os.utime(expired_malformed, (old_timestamp, old_timestamp))

    leases = service.list_leases()

    assert leases == []
    assert service.last_cleanup_counts == {
        "pruned_expired_leases": 0,
        "malformed_leases": 2,
        "pruned_malformed_leases": 1,
    }
    assert fresh_malformed.exists() is True
    assert expired_malformed.exists() is False
