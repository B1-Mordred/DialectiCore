import os
import time
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.domain.schemas import WorkerHeartbeatRequest, WorkerLeaseRecord
from app.services.worker_status_service import (
    WorkerStatusService,
    configured_worker_roles,
)


def test_worker_status_service_records_and_updates_heartbeat(tmp_path) -> None:
    service = WorkerStatusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_heartbeat_ttl_seconds=30,
            worker_required_roles="workflow-worker,voicebox-adapter",
        )
    )

    first = service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="voicebox-adapter",
            worker_id="worker-1",
            status="running",
            details={"pending_audio_assets": 3},
        )
    )
    second = service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="voicebox-adapter",
            worker_id="worker-1",
            status="idle",
            details={"pending_audio_assets": 0},
        )
    )

    summary = service.summary()
    assert first.first_seen_at == second.first_seen_at
    assert summary.status == "degraded"
    assert summary.counts["active_workers"] == 1
    assert summary.counts["active_roles"] == 1
    assert summary.counts["pruned_expired_leases"] == 0
    assert summary.workers[0].status == "idle"
    assert summary.workers[0].details["pending_audio_assets"] == 0


def test_worker_status_service_includes_pruned_expired_lease_count(tmp_path) -> None:
    service = WorkerStatusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_heartbeat_ttl_seconds=30,
        )
    )

    summary = service.summary(
        lease_cleanup_counts={
            "pruned_expired_leases": 2,
            "malformed_leases": 0,
            "pruned_malformed_leases": 0,
        }
    )

    assert summary.counts["pruned_expired_leases"] == 2


def test_worker_status_service_is_healthy_when_all_roles_are_active(tmp_path) -> None:
    settings = Settings(
        runtime_state_path=str(tmp_path / "runtime-state"),
        worker_heartbeat_ttl_seconds=30,
    )
    service = WorkerStatusService(settings)

    for role in configured_worker_roles(settings):
        service.record_heartbeat(
            WorkerHeartbeatRequest(
                role=role,
                worker_id=f"{role}-1",
                status="idle",
            )
        )

    summary = service.summary()

    assert summary.status == "healthy"
    assert summary.counts["active_workers"] == len(configured_worker_roles(settings))
    assert summary.counts["active_roles"] == len(configured_worker_roles(settings))
    assert summary.counts["configured_roles"] == len(configured_worker_roles(settings))


def test_worker_status_service_ignores_replaced_stale_role_records(tmp_path) -> None:
    settings = Settings(
        runtime_state_path=str(tmp_path / "runtime-state"),
        worker_heartbeat_ttl_seconds=1,
    )
    service = WorkerStatusService(settings)

    stale = service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="render-old",
            status="running",
        )
    )
    stale_path = service._path_for("render-worker", "render-old")
    stale_path.write_text(
        stale.model_copy(
            update={
                "last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=5),
                "heartbeat_age_seconds": 5,
            }
        ).model_dump_json(),
        encoding="utf-8",
    )
    for role in configured_worker_roles(settings):
        service.record_heartbeat(
            WorkerHeartbeatRequest(
                role=role,
                worker_id=f"{role}-fresh",
                status="idle",
            )
        )

    summary = service.summary()

    assert summary.status == "healthy"
    assert summary.counts["stale_workers"] == 0
    assert summary.counts["active_roles"] == len(configured_worker_roles(settings))


def test_worker_status_service_ignores_unconfigured_temporal_worker_health(tmp_path) -> None:
    settings = Settings(runtime_state_path=str(tmp_path / "runtime-state"))
    service = WorkerStatusService(settings)

    for role in configured_worker_roles(settings):
        service.record_heartbeat(
            WorkerHeartbeatRequest(
                role=role,
                worker_id=f"{role}-fresh",
                status="idle",
            )
        )
    service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="temporal-worker",
            worker_id="temporal-local-debug",
            status="degraded",
        )
    )

    summary = service.summary()

    assert summary.status == "healthy"
    assert summary.counts["degraded_workers"] == 0
    assert summary.counts["active_roles"] == len(configured_worker_roles(settings))


def test_worker_status_service_does_not_degrade_on_retained_expired_leases(
    tmp_path,
) -> None:
    settings = Settings(runtime_state_path=str(tmp_path / "runtime-state"))
    service = WorkerStatusService(settings)
    for role in configured_worker_roles(settings):
        service.record_heartbeat(
            WorkerHeartbeatRequest(
                role=role,
                worker_id=f"{role}-fresh",
                status="idle",
            )
        )
    expired_lease = WorkerLeaseRecord(
        role="voicebox-adapter",
        worker_id="old-worker",
        acquired_at=datetime.now(UTC) - timedelta(minutes=5),
        last_renewed_at=datetime.now(UTC) - timedelta(minutes=5),
        expires_at=datetime.now(UTC) - timedelta(minutes=4),
        expired=True,
    )

    summary = service.summary(leases=[expired_lease])

    assert summary.status == "healthy"
    assert summary.counts["expired_leases"] == 1
    assert summary.counts["active_roles"] == len(configured_worker_roles(settings))


def test_worker_status_service_requires_temporal_worker_only_for_external_mode(
    tmp_path,
) -> None:
    local = Settings(runtime_state_path=str(tmp_path / "local"))
    external = Settings(
        runtime_state_path=str(tmp_path / "external"),
        temporal_backend_mode="external",
        temporal_backend_worker_enabled=True,
    )

    assert configured_worker_roles(local) == ["workflow-worker"]
    assert configured_worker_roles(external) == ["workflow-worker", "temporal-worker"]


def test_worker_status_service_marks_stale_heartbeats(tmp_path) -> None:
    service = WorkerStatusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_heartbeat_ttl_seconds=1,
            worker_required_roles="workflow-worker,render-worker",
        )
    )
    record = service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="worker-2",
            status="running",
        )
    )
    stale_record = record.model_copy(
        update={
            "last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=5),
            "heartbeat_age_seconds": 5,
        }
    )
    path = service._path_for("render-worker", "worker-2")
    path.write_text(stale_record.model_dump_json(), encoding="utf-8")

    summary = service.summary()
    assert summary.status == "degraded"
    assert summary.counts["active_workers"] == 0
    assert summary.counts["stale_workers"] == 1
    assert summary.workers[0].stale is True


def test_worker_status_service_prunes_only_expired_heartbeat_records(tmp_path) -> None:
    service = WorkerStatusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_heartbeat_ttl_seconds=1,
            worker_runtime_state_retention_seconds=2,
        )
    )
    record = service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="worker-2",
            status="running",
        )
    )
    expired_record = record.model_copy(
        update={
            "last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=10),
            "heartbeat_age_seconds": 10,
        }
    )
    heartbeat_path = service._path_for("render-worker", "worker-2")
    signal_registry_path = service.root / "signals.json"
    malformed_path = service.root / "malformed.json"
    heartbeat_path.write_text(expired_record.model_dump_json(), encoding="utf-8")
    signal_registry_path.write_text('{"signals":[]}', encoding="utf-8")
    malformed_path.write_text("{not-json", encoding="utf-8")

    summary = service.summary()

    assert summary.counts["pruned_stale_heartbeats"] == 1
    assert summary.counts["malformed_heartbeats"] == 0
    assert summary.counts["pruned_malformed_heartbeats"] == 0
    assert summary.counts["retained_heartbeats"] == 0
    assert summary.workers == []
    assert summary.runtime_state_retention_seconds == 2
    assert heartbeat_path.exists() is False
    assert signal_registry_path.exists() is True
    assert malformed_path.exists() is True


def test_worker_status_service_prunes_only_expired_malformed_heartbeat_files(
    tmp_path,
) -> None:
    service = WorkerStatusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            worker_heartbeat_ttl_seconds=1,
            worker_runtime_state_retention_seconds=2,
        )
    )
    service.root.mkdir(parents=True)
    fresh_malformed = service.root / "render-worker--fresh.json"
    expired_malformed = service.root / "render-worker--expired.json"
    registry_path = service.root / "signals.json"
    fresh_malformed.write_text("{not-json", encoding="utf-8")
    expired_malformed.write_text("{not-json", encoding="utf-8")
    registry_path.write_text("{not-json", encoding="utf-8")
    old_timestamp = time.time() - 10
    os.utime(expired_malformed, (old_timestamp, old_timestamp))
    os.utime(registry_path, (old_timestamp, old_timestamp))

    summary = service.summary()

    assert summary.counts["malformed_heartbeats"] == 2
    assert summary.counts["pruned_malformed_heartbeats"] == 1
    assert fresh_malformed.exists() is True
    assert expired_malformed.exists() is False
    assert registry_path.exists() is True
