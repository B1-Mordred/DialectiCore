from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings
from app.domain.schemas import WorkerHeartbeatRequest
from app.services.worker_status_service import WorkerStatusService
from app.worker_healthcheck import worker_health_status


def test_worker_healthcheck_accepts_fresh_same_host_heartbeat(tmp_path) -> None:
    settings = Settings(
        runtime_state_path=str(tmp_path / "runtime-state"),
        worker_heartbeat_ttl_seconds=30,
    )
    WorkerStatusService(settings).record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="container-a:123",
            status="idle",
        )
    )

    status = worker_health_status(settings, "render-worker", hostname="container-a")

    assert status["role"] == "render-worker"
    assert status["worker_id"] == "container-a:123"
    assert status["status"] == "idle"


def test_worker_healthcheck_rejects_missing_same_host_heartbeat(tmp_path) -> None:
    settings = Settings(runtime_state_path=str(tmp_path / "runtime-state"))
    WorkerStatusService(settings).record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="other-container:123",
            status="running",
        )
    )

    with pytest.raises(RuntimeError, match="no heartbeat"):
        worker_health_status(settings, "render-worker", hostname="container-a")


def test_worker_healthcheck_rejects_stale_heartbeat(tmp_path) -> None:
    settings = Settings(
        runtime_state_path=str(tmp_path / "runtime-state"),
        worker_heartbeat_ttl_seconds=1,
        worker_runtime_state_retention_seconds=60,
    )
    service = WorkerStatusService(settings)
    record = service.record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="container-a:123",
            status="running",
        )
    )
    stale = record.model_copy(
        update={
            "last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=5),
            "heartbeat_age_seconds": 5,
        }
    )
    service._path_for("render-worker", "container-a:123").write_text(
        stale.model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="stale"):
        worker_health_status(settings, "render-worker", hostname="container-a")


def test_worker_healthcheck_rejects_failed_heartbeat(tmp_path) -> None:
    settings = Settings(runtime_state_path=str(tmp_path / "runtime-state"))
    WorkerStatusService(settings).record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id="container-a:123",
            status="failed",
        )
    )

    with pytest.raises(RuntimeError, match="failed"):
        worker_health_status(settings, "render-worker", hostname="container-a")


def test_worker_healthcheck_rejects_unsupported_role(tmp_path) -> None:
    settings = Settings(runtime_state_path=str(tmp_path / "runtime-state"))

    with pytest.raises(RuntimeError, match="not supported"):
        worker_health_status(settings, "typo-worker", hostname="container-a")
