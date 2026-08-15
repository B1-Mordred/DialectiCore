import json
from pathlib import Path

from app.core.config import Settings
from app.domain.schemas import WorkerSignalRequest
from app.services.redis_bus_service import RedisBusService


class FakeRedisClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.streams: list[tuple[str, dict]] = []
        self.stream_entries: list[tuple[str, str, dict]] = []
        self.xadd_options: list[dict] = []

    def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 2

    def xadd(
        self,
        stream: str,
        fields: dict,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        stream_id = f"1700000000000-{len(self.stream_entries)}"
        self.streams.append((stream, fields))
        self.xadd_options.append({"maxlen": maxlen, "approximate": approximate})
        self.stream_entries.append((stream, stream_id, fields))
        return stream_id

    def xrevrange(
        self,
        stream: str,
        max: str = "+",
        min: str = "-",
        count: int = 50,
    ) -> list[tuple[str, dict]]:
        return [
            (stream_id, fields)
            for entry_stream, stream_id, fields in reversed(self.stream_entries)
            if entry_stream == stream
        ][:count]


def test_redis_bus_publishes_system_event_when_enabled(tmp_path: Path) -> None:
    client = FakeRedisClient()
    service = RedisBusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            redis_event_fanout_enabled=True,
            redis_event_channel="dialecticore:test-events",
        ),
        client=client,
    )

    result = service.publish_system_event(
        {"schema_version": "system_status_event.v1", "event_type": "system.snapshot"}
    )

    assert result["status"] == "published"
    assert result["delivered_count"] == 2
    assert client.published[0][0] == "dialecticore:test-events"
    assert json.loads(client.published[0][1])["event_type"] == "system.snapshot"


def test_redis_bus_records_worker_signal_to_stream_and_registry(tmp_path: Path) -> None:
    client = FakeRedisClient()
    service = RedisBusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            redis_worker_signal_enabled=True,
            redis_worker_signal_stream="dialecticore:test-signals",
            redis_worker_signal_maxlen=25,
        ),
        client=client,
    )

    record = service.record_worker_signal(
        WorkerSignalRequest(
            target_role="temporal-worker",
            signal_type="drain",
            reason="maintenance",
            payload={"window": "nightly"},
            user_id="operator",
        )
    )

    assert record["schema_version"] == "worker_signal_delivery.v1"
    assert record["status"] == "queued"
    assert record["redis_stream_id"] == "1700000000000-0"
    assert record["redis_stream_maxlen"] == 25
    assert client.streams[0][0] == "dialecticore:test-signals"
    assert client.xadd_options[0] == {"maxlen": 25, "approximate": True}
    queued = json.loads(client.streams[0][1]["signal"])
    assert queued["target_role"] == "temporal-worker"
    assert queued["signal_type"] == "drain"
    assert queued["status"] == "queued"
    assert queued["redis_stream_maxlen"] == 25
    listed = service.list_worker_signals()
    assert listed[0]["signal_id"] == record["signal_id"]
    assert listed[0]["created_by"] == "operator"
    assert listed[0]["delivery_sources"] == ["runtime_state", "redis_stream"]


def test_redis_bus_lists_redis_stream_signal_without_local_registry(tmp_path: Path) -> None:
    client = FakeRedisClient()
    signal = {
        "schema_version": "worker_signal_delivery.v1",
        "signal_id": "redis-only-signal",
        "target_role": "render-worker",
        "signal_type": "drain",
        "reason": "remote maintenance",
        "created_by": "ops",
        "created_at": "2026-07-27T04:10:00+00:00",
        "status": "queued",
    }
    client.xadd(
        "dialecticore:test-signals",
        {"signal": json.dumps(signal, sort_keys=True, separators=(",", ":"))},
    )
    service = RedisBusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            redis_worker_signal_enabled=True,
            redis_worker_signal_stream="dialecticore:test-signals",
        ),
        client=client,
    )

    latest = service.latest_worker_signal("render-worker")

    assert latest is not None
    assert latest["signal_id"] == "redis-only-signal"
    assert latest["signal_type"] == "drain"
    assert latest["redis_stream_id"] == "1700000000000-0"
    assert latest["delivery_sources"] == ["redis_stream"]


def test_redis_bus_records_disabled_worker_signal_locally(tmp_path: Path) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))

    record = service.record_worker_signal(
        WorkerSignalRequest(target_role="workflow-worker", signal_type="reload")
    )

    assert record["status"] == "disabled"
    assert record["redis_enabled"] is False
    assert service.list_worker_signals()[0]["target_role"] == "workflow-worker"


def test_redis_bus_worker_signal_registry_uses_configured_retention(
    tmp_path: Path,
) -> None:
    service = RedisBusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            redis_worker_signal_maxlen=2,
        )
    )
    first = service.record_worker_signal(
        WorkerSignalRequest(target_role="workflow-worker", signal_type="reload")
    )
    second = service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="drain")
    )
    third = service.record_worker_signal(
        WorkerSignalRequest(target_role="publishing-worker", signal_type="resume")
    )

    listed = service.list_worker_signals(limit=10)
    registry = json.loads(
        (tmp_path / "runtime-state" / "workers" / "signals.json").read_text(encoding="utf-8")
    )
    summary = service.worker_signal_summary(limit=10)

    assert [signal["signal_id"] for signal in listed] == [
        third["signal_id"],
        second["signal_id"],
    ]
    assert first["signal_id"] not in {signal["signal_id"] for signal in listed}
    assert [signal["signal_id"] for signal in registry["signals"]] == [
        second["signal_id"],
        third["signal_id"],
    ]
    assert service.latest_worker_signal("workflow-worker") is None
    assert summary["limit"] == 2
    assert summary["requested_limit"] == 10
    assert summary["retention_maxlen"] == 2
    assert summary["recent_count"] == 2


def test_redis_bus_latest_worker_signal_uses_latest_targeted_record(tmp_path: Path) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    drain = service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="drain")
    )
    resume = service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="resume")
    )

    latest = service.latest_worker_signal("render-worker")

    assert latest is not None
    assert latest["signal_id"] == resume["signal_id"]
    assert latest["signal_id"] != drain["signal_id"]
    assert latest["signal_type"] == "resume"
    summary = service.worker_signal_summary()
    assert summary["blocking_count"] == 0
    assert summary["active_blocking_target_roles"] == []
    assert summary["by_active_blocking_target_role"] == {}


def test_redis_bus_invalid_signal_type_does_not_clear_older_active_block(
    tmp_path: Path,
) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    drain = service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="drain")
    )
    registry_path = tmp_path / "runtime-state" / "workers" / "signals.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["signals"].append(
        {
            "schema_version": "worker_signal_delivery.v1",
            "signal_id": "invalid-latest-resume",
            "target_role": "render-worker",
            "signal_type": "not-a-worker-signal",
            "status": "queued",
            "created_at": "9999-01-01T00:00:00+00:00",
        }
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    latest = service.latest_worker_signal("render-worker")
    summary = service.worker_signal_summary()

    assert latest is not None
    assert latest["signal_id"] == drain["signal_id"]
    assert summary["recent_count"] == 2
    assert summary["malformed_count"] == 1
    assert summary["blocking_count"] == 1
    assert summary["active_blocking_target_roles"] == ["render-worker"]
    assert summary["latest_signal"]["signal_id"] == drain["signal_id"]


def test_redis_bus_invalid_signal_type_does_not_create_active_block(
    tmp_path: Path,
) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    registry_path = tmp_path / "runtime-state" / "workers" / "signals.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "worker_signal_registry.v1",
                "signals": [
                    {
                        "schema_version": "worker_signal_delivery.v1",
                        "signal_id": "invalid-drain",
                        "target_role": "render-worker",
                        "signal_type": "drain-now",
                        "status": "queued",
                        "created_at": "9999-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert service.latest_worker_signal("render-worker") is None
    summary = service.worker_signal_summary()
    assert summary["recent_count"] == 1
    assert summary["malformed_count"] == 1
    assert summary["blocking_count"] == 0
    assert summary["active_blocking_target_roles"] == []
    assert summary["latest_signal"] is None


def test_redis_bus_worker_signal_summary_counts_active_blocks_by_target(
    tmp_path: Path,
) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="drain")
    )
    service.record_worker_signal(
        WorkerSignalRequest(target_role="voicebox-adapter", signal_type="stop_after_current")
    )
    service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="resume")
    )

    summary = service.worker_signal_summary()

    assert summary["recent_count"] == 3
    assert summary["blocking_count"] == 1
    assert summary["active_blocking_target_roles"] == ["voicebox-adapter"]
    assert summary["by_active_blocking_target_role"] == {"voicebox-adapter": 1}
    assert summary["by_signal_type"] == {
        "drain": 1,
        "resume": 1,
        "stop_after_current": 1,
    }


def test_redis_bus_worker_signal_summary_wildcard_resume_clears_older_blocks(
    tmp_path: Path,
) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="drain")
    )
    service.record_worker_signal(
        WorkerSignalRequest(target_role="voicebox-adapter", signal_type="stop_after_current")
    )
    service.record_worker_signal(WorkerSignalRequest(target_role="*", signal_type="resume"))

    summary = service.worker_signal_summary()

    assert summary["recent_count"] == 3
    assert summary["blocking_count"] == 0
    assert summary["active_blocking_target_roles"] == []
    assert summary["by_active_blocking_target_role"] == {}


def test_redis_bus_worker_signal_summary_reports_wildcard_blocker(
    tmp_path: Path,
) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="resume")
    )
    service.record_worker_signal(WorkerSignalRequest(target_role="*", signal_type="drain"))

    summary = service.worker_signal_summary()

    assert summary["blocking_count"] == 1
    assert summary["active_blocking_target_roles"] == ["*"]
    assert summary["by_active_blocking_target_role"] == {"*": 1}


def test_redis_bus_latest_worker_signal_supports_wildcard_target(tmp_path: Path) -> None:
    service = RedisBusService(Settings(runtime_state_path=str(tmp_path / "runtime-state")))
    record = service.record_worker_signal(
        WorkerSignalRequest(target_role="*", signal_type="stop_after_current")
    )

    latest = service.latest_worker_signal("temporal-worker")

    assert latest is not None
    assert latest["signal_id"] == record["signal_id"]
    assert latest["signal_type"] == "stop_after_current"


def test_redis_bus_worker_signal_summary_counts_status_type_and_sources(
    tmp_path: Path,
) -> None:
    client = FakeRedisClient()
    service = RedisBusService(
        Settings(
            runtime_state_path=str(tmp_path / "runtime-state"),
            redis_worker_signal_enabled=True,
            redis_worker_signal_stream="dialecticore:test-signals",
        ),
        client=client,
    )

    record = service.record_worker_signal(
        WorkerSignalRequest(target_role="render-worker", signal_type="drain")
    )
    summary = service.worker_signal_summary()

    assert summary["schema_version"] == "worker_signal_summary.v1"
    assert summary["recent_count"] == 1
    assert summary["blocking_count"] == 1
    assert summary["active_blocking_target_roles"] == ["render-worker"]
    assert summary["by_active_blocking_target_role"] == {"render-worker": 1}
    assert summary["failed_count"] == 0
    assert summary["by_status"]["queued"] == 1
    assert summary["by_signal_type"]["drain"] == 1
    assert summary["by_target_role"]["render-worker"] == 1
    assert summary["by_delivery_source"]["runtime_state"] == 1
    assert summary["by_delivery_source"]["redis_stream"] == 1
    assert summary["latest_signal"]["signal_id"] == record["signal_id"]
