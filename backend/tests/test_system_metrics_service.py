from datetime import UTC, datetime, timedelta

from app.domain.schemas import WorkerLeaseRecord, WorkerStatusRecord, WorkerStatusSummary
from app.services.system_metrics_service import SystemMetricsService


def test_local_object_storage_metrics_use_normalized_writability_key() -> None:
    health = {
        "status": "degraded",
        "components": [
            {
                "name": "object_storage",
                "status": "degraded",
                "details": {
                    "backend": "local",
                    "bucket": "dialecticore",
                    "checked_path": "/tmp/dialecticore-object-store",
                    "checked_path_exists": True,
                    "checked_path_is_dir": True,
                    "writable_target_or_parent": False,
                    "writable_parent": True,
                    "readiness_checks": {
                        "checked_path_exists": True,
                        "checked_path_is_directory": True,
                        "writable_target_or_parent": False,
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert (
        "dialecticore_object_storage_local_path_ready"
        '{backend="local",bucket="dialecticore",'
        'checked_path="/tmp/dialecticore-object-store"} 0'
    ) in metrics
    assert (
        "dialecticore_object_storage_local_path_state"
        '{backend="local",bucket="dialecticore",'
        'checked_path="/tmp/dialecticore-object-store",'
        'state="writable_target_or_parent"} 0'
    ) in metrics


def test_publish_package_manifest_metrics_use_health_counts() -> None:
    health = {
        "status": "healthy",
        "components": [],
        "counts": {
            "publish_jobs": 0,
            "completed_export_packages": 3,
            "production_manifest_assets": 2,
            "invalid_production_manifest_assets": 4,
            "packages_missing_package_qc": 5,
            "packages_failing_package_qc": 6,
            "packages_missing_thumbnail": 7,
            "packages_missing_subtitles": 8,
            "packages_missing_production_manifest": 1,
        },
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert (
        'dialecticore_publish_package_manifest_count{kind="completed_export_packages"} 3'
        in metrics
    )
    assert (
        'dialecticore_publish_package_manifest_count{kind="production_manifest_assets"} 2'
        in metrics
    )
    assert (
        "dialecticore_publish_package_manifest_count"
        '{kind="invalid_production_manifest_assets"} 4'
        in metrics
    )
    assert (
        "dialecticore_publish_package_manifest_count"
        '{kind="packages_missing_package_qc"} 5'
        in metrics
    )
    assert (
        "dialecticore_publish_package_manifest_count"
        '{kind="packages_failing_package_qc"} 6'
        in metrics
    )
    assert (
        "dialecticore_publish_package_manifest_count"
        '{kind="packages_missing_thumbnail"} 7'
        in metrics
    )
    assert (
        "dialecticore_publish_package_manifest_count"
        '{kind="packages_missing_subtitles"} 8'
        in metrics
    )
    assert (
        "dialecticore_publish_package_manifest_count"
        '{kind="packages_missing_production_manifest"} 1'
        in metrics
    )


def test_backup_storage_metrics_use_health_component() -> None:
    health = {
        "status": "warning",
        "components": [
            {
                "name": "backup_storage",
                "status": "warning",
                "details": {
                    "archive_count": 5,
                    "readable_archive_count": 4,
                    "restore_validated_archive_count": 3,
                    "restore_unvalidated_archive_count": 1,
                    "unreadable_archive_count": 1,
                    "latest_archive": {
                        "filename": 'backup-"latest".tar.gz',
                        "manifest_readable": True,
                        "age_seconds": 123.4567,
                        "size_bytes": 987654,
                    },
                    "latest_restore_validation": {
                        "backup_id": "backup-001",
                        "status": "validated",
                        "validated": True,
                        "restore_plan_schema_version": "backup_restore_plan.v1",
                        "validation_age_seconds": 45.6789,
                        "object_storage_archive_validation": {
                            "status": "validated",
                            "validated": True,
                            "schema_version": "file_storage_restore_validation.v1",
                        },
                        "runtime_state_archive_validation": {
                            "status": "checksum_mismatch",
                            "validated": False,
                            "schema_version": "file_storage_restore_validation.v1",
                        },
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert "dialecticore_backup_archive_count 5" in metrics
    assert 'dialecticore_backup_archive_validation_count{status="readable"} 4' in metrics
    assert 'dialecticore_backup_archive_validation_count{status="validated"} 3' in metrics
    assert 'dialecticore_backup_archive_validation_count{status="unvalidated"} 1' in metrics
    assert 'dialecticore_backup_archive_validation_count{status="unreadable"} 1' in metrics
    assert (
        "dialecticore_backup_latest_archive_info"
        '{filename="backup-\\"latest\\".tar.gz",manifest_readable="true"} 1'
    ) in metrics
    assert "dialecticore_backup_latest_age_seconds 123.457" in metrics
    assert "dialecticore_backup_latest_size_bytes 987654" in metrics
    assert (
        "dialecticore_backup_latest_restore_validated"
        '{backup_id="backup-001",status="validated",'
        'restore_plan_schema_version="backup_restore_plan.v1"} 1'
    ) in metrics
    assert "dialecticore_backup_latest_restore_validation_age_seconds 45.679" in metrics
    assert (
        "dialecticore_backup_latest_content_validation"
        '{scope="object_storage",status="validated",'
        'schema_version="file_storage_restore_validation.v1"} 1'
    ) in metrics
    assert (
        "dialecticore_backup_latest_content_validation"
        '{scope="runtime_state",status="checksum_mismatch",'
        'schema_version="file_storage_restore_validation.v1"} 0'
    ) in metrics


def test_production_run_metrics_use_health_component() -> None:
    health = {
        "status": "degraded",
        "components": [
            {
                "name": "production_runs",
                "status": "degraded",
                "details": {
                    "production_run_count": 7,
                    "active_production_runs": 6,
                    "running_active_production_runs": 5,
                    "paused_active_production_runs": 4,
                    "failed_active_production_runs": 3,
                    "cancelled_active_production_runs": 2,
                    "completion_blocked_production_runs": 1,
                    "attention_count": 6,
                    "by_completion_failed_check": {
                        "final_render_source_assets_stale": 2,
                        "publish_job_missing": 1,
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert 'dialecticore_production_run_health_status{status="degraded"} 1' in metrics
    assert 'dialecticore_production_run_count{kind="total"} 7' in metrics
    assert 'dialecticore_production_run_count{kind="active"} 6' in metrics
    assert 'dialecticore_production_run_count{kind="running_active"} 5' in metrics
    assert 'dialecticore_production_run_count{kind="paused_active"} 4' in metrics
    assert 'dialecticore_production_run_count{kind="failed_active"} 3' in metrics
    assert 'dialecticore_production_run_count{kind="cancelled_active"} 2' in metrics
    assert 'dialecticore_production_run_count{kind="completion_blocked"} 1' in metrics
    assert 'dialecticore_production_run_count{kind="attention"} 6' in metrics
    assert (
        'dialecticore_production_run_count{kind="completion_failed_check",'
        'check="final_render_source_assets_stale"} 2'
    ) in metrics
    assert (
        'dialecticore_production_run_count{kind="completion_failed_check",'
        'check="publish_job_missing"} 1'
    ) in metrics


def test_worker_runtime_and_signal_metrics_use_health_evidence() -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    health = {
        "status": "degraded",
        "components": [],
        "counts": {},
        "queues": {},
        "settings": {},
        "worker_signals": {
            "recent_count": 4,
            "blocking_count": 1,
            "failed_count": 2,
            "malformed_count": 3,
            "by_status": {"failed": 2, "queued": 2},
            "by_signal_type": {"drain": 1, "resume": 1, "stop_after_current": 2},
            "by_target_role": {"voicebox-adapter": 3, "*": 1},
            "by_active_blocking_target_role": {"voicebox-adapter": 1},
            "by_delivery_source": {"local": 1, "redis_stream": 3},
        },
    }
    workers = WorkerStatusSummary(
        status="degraded",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={
            "active_workers": 1,
            "stale_workers": 1,
            "malformed_heartbeats": 2,
            "malformed_leases": 3,
        },
        workers=[
            WorkerStatusRecord(
                role="voicebox-adapter",
                worker_id="worker-a",
                status="degraded",
                first_seen_at=now - timedelta(minutes=5),
                last_heartbeat_at=now - timedelta(seconds=12),
                heartbeat_age_seconds=12.3456,
                stale=False,
            )
        ],
        leases=[
            WorkerLeaseRecord(
                role="voicebox-adapter",
                worker_id="worker-a",
                acquired_at=now - timedelta(seconds=20),
                last_renewed_at=now - timedelta(seconds=5),
                expires_at=now + timedelta(seconds=30),
                lease_age_seconds=20,
                expires_in_seconds=30.125,
                expired=False,
            )
        ],
    )

    metrics = SystemMetricsService().render(health, workers)

    assert 'dialecticore_worker_count{kind="active_workers"} 1' in metrics
    assert 'dialecticore_worker_count{kind="stale_workers"} 1' in metrics
    assert 'dialecticore_worker_count{kind="malformed_heartbeats"} 2' in metrics
    assert 'dialecticore_worker_count{kind="malformed_leases"} 3' in metrics
    assert 'dialecticore_worker_runtime_seconds{kind="heartbeat_ttl"} 90' in metrics
    assert 'dialecticore_worker_runtime_seconds{kind="lease_ttl"} 45' in metrics
    assert (
        'dialecticore_worker_runtime_seconds{kind="runtime_state_retention"} 86400'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="recent",value="all"} 4'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="blocking",value="all"} 1'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="failed",value="all"} 2'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="malformed",value="all"} 3'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="status",value="failed"} 2'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="signal_type",value="stop_after_current"} 2'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="target_role",value="voicebox-adapter"} 3'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="active_blocking_target_role",value="voicebox-adapter"} 1'
        in metrics
    )
    assert (
        "dialecticore_worker_signal_count"
        '{dimension="delivery_source",value="redis_stream"} 3'
        in metrics
    )
    assert (
        "dialecticore_worker_heartbeat_age_seconds"
        '{role="voicebox-adapter",worker_id="worker-a",status="degraded",stale="false"} 12.346'
        in metrics
    )
    assert (
        "dialecticore_worker_lease_expires_in_seconds"
        '{role="voicebox-adapter",worker_id="worker-a",expired="false"} 30.125'
        in metrics
    )


def test_workflow_orchestration_metrics_include_stage_breakdowns() -> None:
    health = {
        "status": "degraded",
        "components": [
            {
                "name": "workflow_orchestration",
                "status": "degraded",
                "details": {
                    "attempt_count": 2,
                    "progressed_stage_count": 5,
                    "failed_stage_count": 3,
                    "error_count": 1,
                    "dispatch_count": 4,
                    "blocked_dispatch_count": 2,
                    "ready_dispatch_count": 2,
                    "production_handoff_count": 3,
                    "blocked_production_handoff_count": 1,
                    "review_ready_production_handoff_count": 1,
                    "delivery_ready_production_handoff_count": 1,
                    "by_worker": {"workflow-worker": 2},
                    "by_policy": {"ordered": 2},
                    "by_dispatch_status": {"blocked": 2, "ready": 2},
                    "by_failed_stage": {"voicebox": 2, "render": 1},
                    "by_progressed_stage": {"research": 1, "discussion": 4},
                    "by_blocked_dispatch_stage": {"publishing": 1, "render": 1},
                    "by_ready_dispatch_stage": {"timeline": 2},
                    "by_production_handoff_status": {
                        "blocked": 1,
                        "delivery_ready": 1,
                        "review_ready": 1,
                    },
                    "by_production_handoff_blocker": {"completed_audio_missing": 1},
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert (
        'dialecticore_workflow_orchestration_count{dimension="attempts",value="all"} 2'
        in metrics
    )
    assert (
        'dialecticore_workflow_orchestration_count{dimension="stages",value="progressed"} 5'
        in metrics
    )
    assert (
        'dialecticore_workflow_orchestration_count{dimension="stages",value="failed"} 3'
        in metrics
    )
    assert (
        'dialecticore_workflow_orchestration_count{dimension="dispatch",value="blocked"} 2'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="production_handoff",value="blocked"} 1'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="production_handoff",value="delivery_ready"} 1'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="failed_stage",value="voicebox"} 2'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="progressed_stage",value="discussion"} 4'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="blocked_dispatch_stage",value="publishing"} 1'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="ready_dispatch_stage",value="timeline"} 2'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="production_handoff_status",value="review_ready"} 1'
        in metrics
    )
    assert (
        "dialecticore_workflow_orchestration_count"
        '{dimension="production_handoff_blocker",value="completed_audio_missing"} 1'
        in metrics
    )


def test_workflow_retry_metrics_include_schedule_stage_breakdowns() -> None:
    health = {
        "status": "degraded",
        "components": [
            {
                "name": "workflow_retries",
                "status": "degraded",
                "details": {
                    "total_retry_entries": 4,
                    "historical_retry_entries": 3,
                    "resolved_retry_entries": 2,
                    "by_status": {"scheduled": 3, "exhausted": 1},
                    "by_stage": {"voicebox": 2, "render": 2},
                    "by_schedule_status": {"due": 1, "backoff": 1, "unknown": 1},
                    "by_due_stage": {"voicebox": 1},
                    "by_backoff_stage": {"render": 1},
                    "by_unknown_schedule_stage": {"timeline": 1},
                    "by_exhausted_stage": {"render": 1},
                    "by_resolution_status": {"operator_retried": 1},
                    "by_resolution_stage": {"voicebox": 1},
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert 'dialecticore_workflow_stage_retry_count{dimension="total",value="all"} 4' in metrics
    assert 'dialecticore_workflow_stage_retry_count{dimension="history",value="all"} 3' in metrics
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="history_status",value="resolved"} 2'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="status",value="scheduled"} 3'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="schedule_status",value="due"} 1'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="due_stage",value="voicebox"} 1'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="backoff_stage",value="render"} 1'
        in metrics
    )
    assert (
        "dialecticore_workflow_stage_retry_count"
        '{dimension="unknown_schedule_stage",value="timeline"} 1'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="exhausted_stage",value="render"} 1'
        in metrics
    )
    assert (
        "dialecticore_workflow_stage_retry_count"
        '{dimension="resolution_status",value="operator_retried"} 1'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_retry_count{dimension="resolution_stage",value="voicebox"} 1'
        in metrics
    )


def test_media_queue_metrics_use_health_queue_breakdowns() -> None:
    health = {
        "status": "degraded",
        "components": [],
        "counts": {},
        "queues": {
            "pending_audio_jobs": 3,
            "submitted_audio_jobs": 2,
            "running_audio_jobs": 1,
            "pending_visual_jobs": 5,
            "submitted_visual_jobs": 4,
            "running_visual_jobs": 1,
            "pending_subtitle_jobs": 7,
            "submitted_subtitle_jobs": 6,
            "running_subtitle_jobs": 1,
            "failed_assets": 2,
            "failed_audio_assets": 1,
            "failed_visual_assets": 1,
            "failed_subtitle_assets": 0,
        },
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert 'dialecticore_queue_count{kind="pending_audio_jobs"} 3' in metrics
    assert 'dialecticore_queue_count{kind="submitted_audio_jobs"} 2' in metrics
    assert 'dialecticore_queue_count{kind="running_audio_jobs"} 1' in metrics
    assert 'dialecticore_queue_count{kind="pending_visual_jobs"} 5' in metrics
    assert 'dialecticore_queue_count{kind="submitted_visual_jobs"} 4' in metrics
    assert 'dialecticore_queue_count{kind="running_visual_jobs"} 1' in metrics
    assert 'dialecticore_queue_count{kind="pending_subtitle_jobs"} 7' in metrics
    assert 'dialecticore_queue_count{kind="submitted_subtitle_jobs"} 6' in metrics
    assert 'dialecticore_queue_count{kind="running_subtitle_jobs"} 1' in metrics
    assert 'dialecticore_queue_count{kind="failed_assets"} 2' in metrics
    assert 'dialecticore_queue_count{kind="failed_audio_assets"} 1' in metrics
    assert 'dialecticore_queue_count{kind="failed_visual_assets"} 1' in metrics
    assert 'dialecticore_queue_count{kind="failed_subtitle_assets"} 0' in metrics


def test_model_generation_observability_metrics_use_health_component() -> None:
    health = {
        "status": "healthy",
        "components": [
            {
                "name": "model_generation_observability",
                "status": "healthy",
                "details": {
                    "turn_count": 2,
                    "latency_recorded_turn_count": 2,
                    "token_usage_recorded_turn_count": 1,
                    "model_latency_sum_ms": 123.456,
                    "total_prompt_tokens": 12,
                    "total_completion_tokens": 5,
                    "total_tokens": 17,
                    "by_provider_type": {
                        "openai_compatible": {
                            "turn_count": 1,
                            "latency_recorded_turn_count": 1,
                            "token_usage_recorded_turn_count": 1,
                            "latency_sum_ms": 100.0,
                            "prompt_tokens": 12,
                            "completion_tokens": 5,
                            "total_tokens": 17,
                        },
                        "mock": {
                            "turn_count": 1,
                            "latency_recorded_turn_count": 1,
                            "token_usage_recorded_turn_count": 0,
                            "latency_sum_ms": 23.456,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        },
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert "dialecticore_model_generation_turn_count 2" in metrics
    assert "dialecticore_model_generation_latency_ms_sum 123.456" in metrics
    assert "dialecticore_model_generation_latency_ms_count 2" in metrics
    assert "dialecticore_model_generation_token_usage_records 1" in metrics
    assert 'dialecticore_model_generation_token_count{kind="prompt"} 12' in metrics
    assert 'dialecticore_model_generation_token_count{kind="completion"} 5' in metrics
    assert 'dialecticore_model_generation_token_count{kind="total"} 17' in metrics
    assert (
        'dialecticore_model_generation_provider_turn_count{provider_type="mock"} 1'
        in metrics
    )
    assert (
        "dialecticore_model_generation_provider_latency_ms_sum"
        '{provider_type="openai_compatible"} 100.000'
        in metrics
    )
    assert (
        "dialecticore_model_generation_provider_token_count"
        '{provider_type="openai_compatible",kind="total"} 17'
        in metrics
    )


def test_asset_production_observability_metrics_use_health_component() -> None:
    health = {
        "status": "healthy",
        "components": [
            {
                "name": "asset_production_observability",
                "status": "healthy",
                "details": {
                    "asset_count": 3,
                    "completed_asset_count": 2,
                    "failed_asset_count": 1,
                    "duration_recorded_asset_count": 2,
                    "size_recorded_asset_count": 3,
                    "duration_sum_ms": 3000,
                    "storage_size_bytes": 4612,
                    "failure_rate": 0.333333,
                    "by_asset_type": {
                        "audio": {
                            "asset_count": 1,
                            "duration_recorded_asset_count": 1,
                            "duration_sum_ms": 1000,
                            "storage_size_bytes": 4000,
                            "failure_rate": 0.0,
                        },
                        "render": {
                            "asset_count": 1,
                            "duration_recorded_asset_count": 1,
                            "duration_sum_ms": 2000,
                            "storage_size_bytes": 12,
                            "failure_rate": 0.0,
                        },
                    },
                    "by_language": {
                        "de": {
                            "asset_count": 2,
                            "duration_recorded_asset_count": 1,
                            "duration_sum_ms": 2000,
                            "storage_size_bytes": 612,
                            "failure_rate": 0.5,
                        },
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert (
        'dialecticore_production_asset_count{dimension="status",value="all"} 3'
        in metrics
    )
    assert (
        'dialecticore_production_asset_count{dimension="status",value="failed"} 1'
        in metrics
    )
    assert (
        'dialecticore_production_asset_failure_rate{dimension="all",value="all"} 0.333333'
        in metrics
    )
    assert (
        'dialecticore_production_asset_duration_ms_sum{dimension="all",value="all"} 3000'
        in metrics
    )
    assert (
        'dialecticore_production_asset_duration_ms_count{dimension="all",value="all"} 2'
        in metrics
    )
    assert (
        'dialecticore_production_asset_storage_size_bytes{dimension="all",value="all"} 4612'
        in metrics
    )
    assert (
        'dialecticore_production_asset_duration_ms_sum{dimension="asset_type",value="render"} 2000'
        in metrics
    )
    assert (
        'dialecticore_production_asset_failure_rate{dimension="language",value="de"} 0.500000'
        in metrics
    )
    assert (
        'dialecticore_production_asset_storage_size_bytes{dimension="language",value="de"} 612'
        in metrics
    )


def test_workflow_duration_observability_metrics_use_health_component() -> None:
    health = {
        "status": "healthy",
        "components": [
            {
                "name": "workflow_duration_observability",
                "status": "healthy",
                "details": {
                    "production_duration_ms_sum": 12_000,
                    "production_duration_record_count": 1,
                    "stage_duration_ms_sum": 12_000,
                    "stage_duration_record_count": 3,
                    "by_stage": {
                        "DRAFT": {
                            "duration_ms_sum": 5000,
                            "duration_record_count": 1,
                        },
                        "DISCUSSING": {
                            "duration_ms_sum": 4000,
                            "duration_record_count": 1,
                        },
                    },
                    "by_language": {
                        "en": {
                            "duration_ms_sum": 11_000,
                            "duration_record_count": 1,
                        },
                        "de": {
                            "duration_ms_sum": 4000,
                            "duration_record_count": 1,
                        },
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert "dialecticore_episode_production_duration_ms_sum 12000" in metrics
    assert "dialecticore_episode_production_duration_ms_count 1" in metrics
    assert "dialecticore_workflow_stage_duration_ms_sum 12000" in metrics
    assert "dialecticore_workflow_stage_duration_ms_count 3" in metrics
    assert (
        'dialecticore_workflow_stage_duration_ms_sum{stage="DISCUSSING"} 4000'
        in metrics
    )
    assert (
        'dialecticore_workflow_stage_duration_ms_count{stage="DRAFT"} 1'
        in metrics
    )
    assert (
        'dialecticore_language_production_duration_ms_sum{language="en"} 11000'
        in metrics
    )
    assert (
        'dialecticore_language_production_duration_ms_count{language="de"} 1'
        in metrics
    )


def test_queue_wait_observability_metrics_use_health_component() -> None:
    health = {
        "status": "healthy",
        "components": [
            {
                "name": "queue_wait_observability",
                "status": "healthy",
                "details": {
                    "pending_wait_ms_sum": 3000,
                    "pending_wait_record_count": 1,
                    "completed_wait_ms_sum": 21_000,
                    "completed_wait_record_count": 3,
                    "by_queue": {
                        "audio": {
                            "pending_wait_ms_sum": 0,
                            "pending_wait_record_count": 0,
                            "completed_wait_ms_sum": 5000,
                            "completed_wait_record_count": 1,
                        },
                        "publish_job": {
                            "pending_wait_ms_sum": 3000,
                            "pending_wait_record_count": 1,
                            "completed_wait_ms_sum": 9000,
                            "completed_wait_record_count": 1,
                        },
                    },
                    "by_language": {
                        "de": {
                            "pending_wait_ms_sum": 0,
                            "pending_wait_record_count": 0,
                            "completed_wait_ms_sum": 7000,
                            "completed_wait_record_count": 1,
                        }
                    },
                },
            }
        ],
        "counts": {},
        "queues": {},
        "settings": {},
    }
    workers = WorkerStatusSummary(
        status="healthy",
        heartbeat_ttl_seconds=90,
        lease_ttl_seconds=45,
        runtime_state_retention_seconds=86400,
        counts={},
    )

    metrics = SystemMetricsService().render(health, workers)

    assert 'dialecticore_queue_wait_duration_ms_sum{state="pending"} 3000' in metrics
    assert 'dialecticore_queue_wait_duration_ms_count{state="pending"} 1' in metrics
    assert (
        'dialecticore_queue_wait_duration_ms_sum{state="completed"} 21000'
        in metrics
    )
    assert (
        'dialecticore_queue_wait_duration_ms_count{state="completed"} 3'
        in metrics
    )
    assert (
        "dialecticore_queue_wait_duration_ms_sum"
        '{dimension="queue",value="audio",state="completed"} 5000'
        in metrics
    )
    assert (
        "dialecticore_queue_wait_duration_ms_count"
        '{dimension="queue",value="publish_job",state="pending"} 1'
        in metrics
    )
    assert (
        "dialecticore_queue_wait_duration_ms_sum"
        '{dimension="language",value="de",state="completed"} 7000'
        in metrics
    )
