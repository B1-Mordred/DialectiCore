from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.domain.schemas import WorkerSignalRequest

try:
    import redis
except ImportError:  # pragma: no cover - exercised only in minimal local envs
    redis = None


class RedisBusService:
    _SUPPORTED_WORKER_SIGNALS = {"drain", "reload", "resume", "stop_after_current"}

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def publish_system_event(self, payload: dict) -> dict:
        result = {
            "schema_version": "redis_system_event_fanout.v1",
            "enabled": self.settings.redis_event_fanout_enabled,
            "channel": self.settings.redis_event_channel,
            "status": "disabled",
            "published_at": datetime.now(UTC).isoformat(),
        }
        if not self.settings.redis_event_fanout_enabled:
            return result
        client = self._client()
        if client is None:
            return result | {
                "status": "unavailable",
                "reason": "redis package is not available",
            }
        try:
            delivered_count = client.publish(
                self.settings.redis_event_channel,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )
        except Exception as exc:
            return result | {
                "status": "failed",
                "error": type(exc).__name__,
                "reason": str(exc),
            }
        return result | {"status": "published", "delivered_count": int(delivered_count or 0)}

    def record_worker_signal(self, request: WorkerSignalRequest) -> dict:
        record = {
            "schema_version": "worker_signal_delivery.v1",
            "signal_id": str(uuid4()),
            "target_role": request.target_role,
            "signal_type": request.signal_type,
            "reason": request.reason,
            "payload": request.payload,
            "created_by": request.user_id or "system",
            "created_at": datetime.now(UTC).isoformat(),
            "redis_enabled": self.settings.redis_worker_signal_enabled,
            "redis_stream": self.settings.redis_worker_signal_stream,
            "redis_stream_maxlen": self.settings.redis_worker_signal_maxlen,
            "status": "disabled",
        }
        if self.settings.redis_worker_signal_enabled:
            client = self._client()
            if client is None:
                record["status"] = "unavailable"
                record["error"] = "redis package is not available"
            else:
                try:
                    record["status"] = "queued"
                    stream_id = client.xadd(
                        self.settings.redis_worker_signal_stream,
                        {
                            "signal": json.dumps(
                                record,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        },
                        maxlen=self.settings.redis_worker_signal_maxlen,
                        approximate=True,
                    )
                    record["redis_stream_id"] = (
                        stream_id.decode("utf-8")
                        if isinstance(stream_id, bytes)
                        else str(stream_id)
                    )
                except Exception as exc:
                    record["status"] = "failed"
                    record["error"] = type(exc).__name__
                    record["reason"] = str(exc)
        self._add_delivery_source(record, "runtime_state")
        if record.get("status") == "queued":
            self._add_delivery_source(record, "redis_stream")
        self._append_worker_signal_record(record)
        return record

    def list_worker_signals(self, limit: int = 50) -> list[dict]:
        source_limit = self._worker_signal_record_limit()
        result_limit = self._worker_signal_record_limit(limit)
        records = [
            *self._local_worker_signal_records(limit=source_limit),
            *self._redis_worker_signal_records(limit=source_limit),
        ]
        records = self._dedupe_worker_signal_records(records)
        records.sort(key=self._worker_signal_sort_key, reverse=True)
        return records[:result_limit]

    def latest_worker_signal(self, target_role: str) -> dict | None:
        for signal in self._valid_worker_signal_records(
            self.list_worker_signals(limit=self._worker_signal_record_limit())
        ):
            if signal.get("target_role") in {target_role, "*", "all"}:
                return signal
        return None

    def worker_signal_summary(self, limit: int = 200) -> dict:
        requested_limit = max(1, int(limit))
        effective_limit = self._worker_signal_record_limit(requested_limit)
        signals = self.list_worker_signals(limit=limit)
        valid_signals = self._valid_worker_signal_records(signals)
        latest = valid_signals[0] if valid_signals else None
        return {
            "schema_version": "worker_signal_summary.v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "limit": effective_limit,
            "requested_limit": requested_limit,
            "retention_maxlen": self._worker_signal_record_limit(),
            "recent_count": len(signals),
            "malformed_count": len(signals) - len(valid_signals),
            "by_status": self._count_worker_signal_values(signals, "status"),
            "by_signal_type": self._count_worker_signal_values(signals, "signal_type"),
            "by_target_role": self._count_worker_signal_values(signals, "target_role"),
            "by_delivery_source": self._count_worker_signal_sources(signals),
            "failed_count": sum(
                1 for signal in signals if signal.get("status") in {"failed", "unavailable"}
            ),
            "blocking_count": self._active_blocking_worker_signal_count(valid_signals),
            "active_blocking_target_roles": self._active_blocking_worker_signal_targets(
                valid_signals
            ),
            "by_active_blocking_target_role": self._count_active_blocking_worker_signal_targets(
                valid_signals
            ),
            "latest_signal": self._worker_signal_summary_record(latest),
        }

    def _local_worker_signal_records(self, limit: int = 50) -> list[dict]:
        path = self._worker_signal_registry_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        signals = payload.get("signals", [])
        if not isinstance(signals, list):
            return []
        records = [item for item in signals if isinstance(item, dict)]
        for record in records:
            self._add_delivery_source(record, "runtime_state")
        records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return records[:limit]

    def _redis_worker_signal_records(self, limit: int = 50) -> list[dict]:
        if not self.settings.redis_worker_signal_enabled:
            return []
        client = self._client()
        if client is None:
            return []
        try:
            entries = client.xrevrange(
                self.settings.redis_worker_signal_stream,
                max="+",
                min="-",
                count=limit,
            )
        except Exception:
            return []
        records: list[dict] = []
        for stream_id, fields in entries:
            signal_payload = self._redis_field(fields, "signal")
            if signal_payload is None:
                continue
            try:
                record = json.loads(signal_payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            record.setdefault("schema_version", "worker_signal_delivery.v1")
            record["redis_enabled"] = True
            record["redis_stream"] = self.settings.redis_worker_signal_stream
            record["redis_stream_maxlen"] = self.settings.redis_worker_signal_maxlen
            record["redis_stream_id"] = self._decode_redis_value(stream_id)
            record.setdefault("status", "queued")
            self._add_delivery_source(record, "redis_stream")
            records.append(record)
        return records

    def _dedupe_worker_signal_records(self, records: list[dict]) -> list[dict]:
        merged: dict[str, dict] = {}
        for record in records:
            fallback_key = (
                f"{record.get('created_at')}:{record.get('target_role')}:"
                f"{record.get('signal_type')}"
            )
            key = str(record.get("signal_id") or record.get("redis_stream_id") or fallback_key)
            if key not in merged:
                merged[key] = dict(record)
                continue
            existing = merged[key]
            sources = [
                source
                for source in [
                    *self._delivery_sources(existing),
                    *self._delivery_sources(record),
                ]
            ]
            existing.update({name: value for name, value in record.items() if value is not None})
            existing["delivery_sources"] = list(dict.fromkeys(sources))
        return list(merged.values())

    def _valid_worker_signal_records(self, records: list[dict]) -> list[dict]:
        return [record for record in records if self._worker_signal_record_is_valid(record)]

    def _worker_signal_record_is_valid(self, record: dict) -> bool:
        target_role = record.get("target_role")
        signal_type = record.get("signal_type")
        return (
            isinstance(target_role, str)
            and bool(target_role.strip())
            and len(target_role) <= 128
            and isinstance(signal_type, str)
            and signal_type in self._SUPPORTED_WORKER_SIGNALS
        )

    def _active_blocking_worker_signal_count(self, records: list[dict]) -> int:
        return len(self._active_blocking_worker_signal_records(records))

    def _active_blocking_worker_signal_targets(self, records: list[dict]) -> list[str]:
        return sorted(self._active_blocking_worker_signal_records(records))

    def _count_active_blocking_worker_signal_targets(
        self, records: list[dict]
    ) -> dict[str, int]:
        return {
            target: 1
            for target in self._active_blocking_worker_signal_targets(records)
        }

    def _active_blocking_worker_signal_records(self, records: list[dict]) -> dict[str, dict]:
        active_blockers: dict[str, dict] = {}
        seen_targets: set[str] = set()
        for record in sorted(records, key=self._worker_signal_sort_key, reverse=True):
            target = str(record.get("target_role") or "")
            if not target:
                continue
            if target in {"*", "all"}:
                if record.get("signal_type") in {"drain", "stop_after_current"}:
                    active_blockers[target] = record
                break
            if target in seen_targets:
                continue
            seen_targets.add(target)
            if record.get("signal_type") in {"drain", "stop_after_current"}:
                active_blockers[target] = record
        return active_blockers

    def _worker_signal_sort_key(self, record: dict) -> tuple[str, int, int, str]:
        stream_ms = 0
        stream_seq = 0
        stream_id = record.get("redis_stream_id")
        if isinstance(stream_id, str) and "-" in stream_id:
            first, second = stream_id.split("-", 1)
            if first.isdigit():
                stream_ms = int(first)
            if second.isdigit():
                stream_seq = int(second)
        return (
            str(record.get("created_at", "")),
            stream_ms,
            stream_seq,
            str(record.get("signal_id", "")),
        )

    def _append_worker_signal_record(self, record: dict) -> None:
        limit = self._worker_signal_record_limit()
        signals = self._local_worker_signal_records(limit=limit)
        signals.append(record)
        signals.sort(key=lambda item: str(item.get("created_at", "")))
        signals = signals[-limit:]
        path = self._worker_signal_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "worker_signal_registry.v1",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "signals": signals,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _worker_signal_registry_path(self) -> Path:
        return Path(self.settings.runtime_state_path).expanduser() / "workers" / "signals.json"

    def _worker_signal_record_limit(self, requested: int | None = None) -> int:
        retention_maxlen = max(1, int(self.settings.redis_worker_signal_maxlen))
        if requested is None:
            return retention_maxlen
        return min(retention_maxlen, max(1, int(requested)))

    def _count_worker_signal_values(self, signals: list[dict], field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for signal in signals:
            value = str(signal.get(field) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        return counts

    def _count_worker_signal_sources(self, signals: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for signal in signals:
            sources = self._delivery_sources(signal) or ["unknown"]
            for source in sources:
                counts[source] = counts.get(source, 0) + 1
        return counts

    def _worker_signal_summary_record(self, signal: dict | None) -> dict | None:
        if signal is None:
            return None
        return {
            "signal_id": signal.get("signal_id"),
            "target_role": signal.get("target_role"),
            "signal_type": signal.get("signal_type"),
            "status": signal.get("status"),
            "created_at": signal.get("created_at"),
            "created_by": signal.get("created_by"),
            "redis_stream_id": signal.get("redis_stream_id"),
            "delivery_sources": self._delivery_sources(signal),
        }

    def _redis_field(self, fields: Any, name: str) -> str | None:
        if not isinstance(fields, dict):
            return None
        value = fields.get(name)
        if value is None:
            value = fields.get(name.encode("utf-8"))
        if value is None:
            return None
        return self._decode_redis_value(value)

    def _decode_redis_value(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def _add_delivery_source(self, record: dict, source: str) -> None:
        sources = self._delivery_sources(record)
        if source not in sources:
            sources.append(source)
        record["delivery_sources"] = sources

    def _delivery_sources(self, record: dict) -> list[str]:
        sources = record.get("delivery_sources", [])
        if not isinstance(sources, list):
            return []
        return [source for source in sources if isinstance(source, str)]

    def _client(self) -> Any | None:
        if self.client is not None:
            return self.client
        if redis is None:
            return None
        self.client = redis.Redis.from_url(
            self.settings.redis_url,
            socket_connect_timeout=self.settings.redis_timeout_seconds,
            socket_timeout=self.settings.redis_timeout_seconds,
            decode_responses=False,
        )
        return self.client
