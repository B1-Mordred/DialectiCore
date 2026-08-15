import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus
from app.domain.schemas import (
    Asset,
    AuditEvent,
    DiscussionSession,
    DiscussionTurn,
    Episode,
    PublishJob,
    VoiceboxEndpoint,
)
from app.services import system_health_service as system_health_module
from app.services.system_health_service import SystemHealthService


def test_model_generation_observability_aggregates_persisted_turn_metadata() -> None:
    turns = [
        DiscussionTurn.model_construct(
            generation_metadata={
                "provider_type": "openai_compatible",
                "model_latency_ms": 100.25,
                "token_usage_available": True,
                "token_usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 7,
                    "total_tokens": 17,
                },
            }
        ),
        DiscussionTurn.model_construct(
            generation_metadata={
                "provider_type": "mock",
                "model_latency_ms": 20.0,
                "token_usage_available": False,
                "token_usage": {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                },
            }
        ),
    ]
    episodes = [
        Episode.model_construct(
            discussion_session=DiscussionSession.model_construct(turns=turns)
        )
    ]
    service = SystemHealthService(Settings())

    summary = service._model_generation_observability_summary(episodes)
    component = service._model_generation_observability_check(summary)

    assert component["status"] == "healthy"
    assert summary["turn_count"] == 2
    assert summary["latency_recorded_turn_count"] == 2
    assert summary["token_usage_recorded_turn_count"] == 1
    assert summary["model_latency_sum_ms"] == 120.25
    assert summary["average_model_latency_ms"] == 60.125
    assert summary["total_prompt_tokens"] == 10
    assert summary["total_completion_tokens"] == 7
    assert summary["total_tokens"] == 17
    assert summary["by_provider_type"]["openai_compatible"]["turn_count"] == 1
    assert summary["by_provider_type"]["openai_compatible"]["latency_sum_ms"] == 100.25
    assert summary["by_provider_type"]["mock"]["token_usage_recorded_turn_count"] == 0


def test_model_generation_observability_degrades_for_missing_latency_metadata() -> None:
    episodes = [
        Episode.model_construct(
            discussion_session=DiscussionSession.model_construct(
                turns=[DiscussionTurn.model_construct(generation_metadata={})]
            )
        )
    ]
    service = SystemHealthService(Settings())

    component = service._model_generation_observability_check(
        service._model_generation_observability_summary(episodes)
    )

    assert component["status"] == "degraded"
    assert component["details"]["readiness_checks"] == {
        "model_latency_recorded_for_turns": False,
        "token_usage_aggregation_available": True,
    }
    assert component["details"]["failed_readiness_checks"] == [
        "model_latency_recorded_for_turns"
    ]


def test_media_endpoint_readiness_includes_safe_voice_generation_canary() -> None:
    service = SystemHealthService(Settings())
    endpoint = VoiceboxEndpoint.model_construct(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        health_status="unhealthy",
        capabilities={
            "generation_canary": {
                "status": "fail",
                "checked_at": "2026-07-30T06:58:54Z",
                "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
                "engine": "chatterbox",
                "status_code": 500,
                "content_type": "text/plain; charset=utf-8",
                "bytes": 21,
                "riff_wave": False,
                "text_chars": 42,
                "text": "Guten Tag. This must not be surfaced.",
            }
        },
    )

    entry = service._media_endpoint_readiness_entry(endpoint)

    assert entry["voice_generation"] == {
        "ready": False,
        "status": "fail",
        "status_code": 500,
        "content_type": "text/plain; charset=utf-8",
        "bytes": 21,
        "riff_wave": False,
        "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        "engine": "chatterbox",
        "error_type": None,
        "action": "fix_voicebox_generation_then_rerun_health_check",
    }
    assert "Guten Tag" not in str(entry)
    assert "text_chars" not in str(entry)


def test_stale_failed_managed_media_smoke_is_recheckable_not_a_current_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        system_health_module,
        "managed_media_smoke_evidence",
        lambda _path: {
            "configured": True,
            "status": "runner_failed",
            "ready": False,
            "fresh": False,
            "age_seconds": 172800,
            "action": "run_b1_managed_media_smoke",
        },
    )

    readiness = SystemHealthService(Settings())._managed_media_smoke_readiness()

    assert readiness["status"] == "warning"
    assert readiness["blockers"] == []
    assert readiness["warnings"] == [
        "failed B1 managed media smoke evidence is stale; rerun it"
    ]
    assert readiness["details"]["failed_readiness_checks"] == [
        "managed_media_smoke_passed",
        "managed_media_smoke_fresh",
    ]


def test_busy_managed_media_smoke_is_a_recheck_warning(monkeypatch) -> None:
    monkeypatch.setattr(
        system_health_module,
        "managed_media_smoke_evidence",
        lambda _path: {
            "configured": True,
            "status": "busy",
            "ready": False,
            "fresh": True,
            "action": "wait_for_b1_media_capacity_then_rerun_smoke",
        },
    )

    readiness = SystemHealthService(Settings())._managed_media_smoke_readiness()

    assert readiness["status"] == "warning"
    assert readiness["blockers"] == []
    assert readiness["warnings"] == [
        "B1 managed media scheduler is busy; retry the smoke later"
    ]


def test_readiness_treats_stale_assets_and_active_media_as_nonblocking() -> None:
    now = datetime.now(UTC)
    canonical_transcript_id = uuid4()
    episode_id = uuid4()
    episode = Episode.model_construct(
        id=episode_id,
        status=EpisodeStatus.ready,
        canonical_transcript_version_id=canonical_transcript_id,
        updated_at=now,
        assets=[
            Asset.model_construct(
                id=uuid4(),
                episode_id=episode_id,
                asset_type=AssetType.video,
                status="running",
                generation_metadata={"transcript_version_id": str(canonical_transcript_id)},
            ),
            Asset.model_construct(
                id=uuid4(),
                episode_id=episode_id,
                asset_type=AssetType.broll,
                status="failed",
                generation_metadata={"transcript_version_id": str(uuid4())},
            ),
            Asset.model_construct(
                id=uuid4(),
                episode_id=episode_id,
                asset_type=AssetType.broll,
                status="failed",
                generation_metadata={"transcript_version_id": str(canonical_transcript_id)},
            ),
            Asset.model_construct(
                id=uuid4(),
                episode_id=episode_id,
                asset_type=AssetType.render,
                status="failed",
                generation_metadata={"render_type": "preview"},
            ),
        ],
        workflow_control={
            "run": {
                "run_id": "active-media",
                "state": "running",
                "current_stage": EpisodeStatus.ready.value,
                "updated_at": now.isoformat(),
                "last_worker_orchestration": {
                    "completion_handoff": {
                        "status": "blocked",
                        "failed_checks": ["completed_character_visual_missing"],
                    }
                },
            },
            "worker_orchestration_log": [
                {
                    "recorded_at": now.isoformat(),
                    "worker_id": "workflow-worker",
                    "policy": "test",
                    "production_handoff": {
                        "episode_id": str(episode_id),
                        "status": "blocked",
                        "blocking_reasons": ["completed_character_visual_missing"],
                    },
                }
            ],
        },
    )
    service = SystemHealthService(Settings())

    queue = service._queue_summary([episode])
    run = service._production_run_summary([episode])
    orchestration = service._workflow_orchestration_summary([episode])
    orchestration_readiness = service._workflow_orchestration_readiness(orchestration)

    assert queue["current_failed_assets"] == 0
    assert queue["current_nonblocking_failed_assets"] == 1
    assert queue["current_stale_revision_failed_assets"] == 1
    assert queue["current_failed_render_assets"] == 1
    assert queue["current_pending_visual_jobs"] == 1
    assert service._media_queue_readiness(queue)["status"] == "warning"
    assert service._media_queue_readiness(queue)["blockers"] == []
    assert run["completion_blocked_production_runs"] == 0
    assert run["waiting_for_media_production_runs"] == 1
    assert orchestration["current_blocked_production_handoff_count"] == 0
    assert orchestration["current_waiting_production_handoff_count"] == 1
    assert orchestration_readiness["status"] == "warning"
    assert orchestration_readiness["blockers"] == []
    assert orchestration_readiness["warnings"] == [
        "workflow orchestration is waiting for active media jobs"
    ]

    episode.assets[0].status = "completed"
    no_media_run = service._production_run_summary([episode])
    no_media_orchestration = service._workflow_orchestration_summary([episode])
    no_media_orchestration_readiness = service._workflow_orchestration_readiness(
        no_media_orchestration
    )

    assert no_media_run["completion_blocked_production_runs"] == 0
    assert no_media_run["waiting_for_completion_action_production_runs"] == 1
    assert no_media_orchestration["current_blocked_production_handoff_count"] == 0
    assert no_media_orchestration["current_waiting_action_handoff_count"] == 1
    assert no_media_orchestration_readiness["status"] == "warning"
    assert no_media_orchestration_readiness["blockers"] == []
    assert no_media_orchestration_readiness["warnings"] == [
        "workflow orchestration is waiting for the next production action or review"
    ]


def test_asset_production_observability_aggregates_duration_size_and_rates(tmp_path) -> None:
    local_file = tmp_path / "render.mp4"
    local_file.write_bytes(b"render-bytes")
    assets = [
        Asset.model_construct(
            asset_type=AssetType.audio,
            language="en",
            status="completed",
            duration_ms=1000,
            generation_metadata={"object_size_bytes": 4000},
        ),
        Asset.model_construct(
            asset_type=AssetType.render,
            language="de",
            status="completed",
            duration_ms=2000,
            generation_metadata={"object_storage_path": str(local_file)},
        ),
        Asset.model_construct(
            asset_type=AssetType.video,
            language="de",
            status="failed",
            duration_ms=None,
            generation_metadata={"media_probe": {"size_bytes": 600}},
        ),
    ]
    episodes = [Episode.model_construct(assets=assets)]
    service = SystemHealthService(Settings())

    summary = service._asset_production_observability_summary(episodes)
    component = service._asset_production_observability_check(summary)

    assert component["status"] == "healthy"
    assert summary["asset_count"] == 3
    assert summary["completed_asset_count"] == 2
    assert summary["failed_asset_count"] == 1
    assert summary["duration_recorded_asset_count"] == 2
    assert summary["duration_sum_ms"] == 3000
    assert summary["size_recorded_asset_count"] == 3
    assert summary["storage_size_bytes"] == 4000 + len(b"render-bytes") + 600
    assert summary["failure_rate"] == 0.333333
    assert summary["by_asset_type"]["audio"]["storage_size_bytes"] == 4000
    assert summary["by_asset_type"]["render"]["duration_sum_ms"] == 2000
    assert summary["by_language"]["de"]["asset_count"] == 2
    assert summary["by_language"]["de"]["failed_asset_count"] == 1


def test_backup_storage_check_skips_large_archive_checksum_for_dashboard(
    tmp_path,
    monkeypatch,
) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    archive_path = backup_root / "dialecticore-backup-large.tar.gz"
    manifest = {
        "schema_version": "dialecticore.backup.v1",
        "backup_id": "dialecticore-backup-large",
        "created_at": datetime.now(UTC).isoformat(),
        "database": {"total_records": 2},
        "object_storage": {"file_count": 1},
        "runtime_state": {"file_count": 0},
    }
    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_payload = json.dumps(manifest).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))
        payload = b"x" * 2048
        payload_info = tarfile.TarInfo("object-storage/large.bin")
        payload_info.size = len(payload)
        archive.addfile(payload_info, io.BytesIO(payload))
    import app.services.system_health_service as health_module

    monkeypatch.setattr(health_module, "BACKUP_HEALTH_CHECKSUM_MAX_BYTES", 1)
    service = SystemHealthService(Settings(backup_path=str(backup_root)))
    validation_event = AuditEvent(
        event_type="backup.restore_validated",
        actor="tester",
        details={
            "backup_id": manifest["backup_id"],
            "archive_path": str(archive_path),
            "archive_checksum": "sha256:validated",
            "restore_plan": {"schema_version": "backup_restore_plan.v1", "summary": {}},
        },
    )

    component = service._backup_storage_check([validation_event])

    assert component["details"]["latest_archive"]["checksum_status"] == "skipped"
    assert component["details"]["latest_archive"]["archive_checksum"] is None
    assert component["details"]["latest_restore_validation"]["status"] == "checksum_not_evaluated"
    assert component["details"]["readiness_checks"]["backup_archives_readable"] is True
    assert (
        "latest_restore_validation_current"
        in component["details"]["failed_readiness_checks"]
    )


def test_workflow_duration_observability_aggregates_run_stage_and_language_spans() -> None:
    base = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    episode = Episode.model_construct(
        workflow_control={
            "run": {
                "started_at": base.isoformat(),
                "updated_at": (base + timedelta(seconds=12)).isoformat(),
                "completed_at": (base + timedelta(seconds=12)).isoformat(),
                "stage_history": [
                    {"stage": "DRAFT", "entered_at": base.isoformat()},
                    {
                        "stage": "DISCUSSING",
                        "entered_at": (base + timedelta(seconds=5)).isoformat(),
                    },
                    {
                        "stage": "TRANSCRIPT_REVIEW",
                        "entered_at": (base + timedelta(seconds=9)).isoformat(),
                    },
                ],
            }
        },
        assets=[
            Asset.model_construct(
                asset_type=AssetType.audio,
                language="en",
                created_at=base + timedelta(seconds=20),
                updated_at=base + timedelta(seconds=23),
                generation_metadata={},
            ),
            Asset.model_construct(
                asset_type=AssetType.render,
                language="en",
                created_at=base + timedelta(seconds=21),
                updated_at=base + timedelta(seconds=31),
                generation_metadata={},
            ),
            Asset.model_construct(
                asset_type=AssetType.subtitle,
                language="de",
                created_at=base + timedelta(seconds=40),
                updated_at=base + timedelta(seconds=44),
                generation_metadata={},
            ),
        ],
    )
    service = SystemHealthService(Settings())

    summary = service._workflow_duration_observability_summary([episode])
    component = service._workflow_duration_observability_check(summary)

    assert component["status"] == "healthy"
    assert summary["production_duration_ms_sum"] == 12_000
    assert summary["production_duration_record_count"] == 1
    assert summary["stage_duration_ms_sum"] == 12_000
    assert summary["stage_duration_record_count"] == 3
    assert summary["by_stage"]["DRAFT"] == {
        "duration_ms_sum": 5000,
        "duration_record_count": 1,
    }
    assert summary["by_stage"]["DISCUSSING"] == {
        "duration_ms_sum": 4000,
        "duration_record_count": 1,
    }
    assert summary["by_stage"]["TRANSCRIPT_REVIEW"] == {
        "duration_ms_sum": 3000,
        "duration_record_count": 1,
    }
    assert summary["by_language"]["en"] == {
        "duration_ms_sum": 11_000,
        "duration_record_count": 1,
    }
    assert summary["by_language"]["de"] == {
        "duration_ms_sum": 4000,
        "duration_record_count": 1,
    }


def test_queue_wait_observability_aggregates_assets_and_publish_jobs() -> None:
    base = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    episode = Episode.model_construct(
        assets=[
            Asset.model_construct(
                asset_type=AssetType.audio,
                language="en",
                status="completed",
                created_at=base,
                updated_at=base + timedelta(seconds=5),
                generation_metadata={
                    "submitted_at": base.isoformat(),
                    "completed_at": (base + timedelta(seconds=5)).isoformat(),
                },
            ),
            Asset.model_construct(
                asset_type=AssetType.video,
                language="de",
                status="completed",
                created_at=base + timedelta(seconds=10),
                updated_at=base + timedelta(seconds=17),
                generation_metadata={
                    "submitted_at": (base + timedelta(seconds=10)).isoformat(),
                    "completed_at": (base + timedelta(seconds=17)).isoformat(),
                },
            ),
        ],
        publish_jobs=[
            PublishJob.model_construct(
                status="completed",
                requested_at=base + timedelta(seconds=20),
                completed_at=base + timedelta(seconds=29),
            )
        ],
    )
    service = SystemHealthService(Settings())

    summary = service._queue_wait_observability_summary([episode])
    component = service._queue_wait_observability_check(summary)

    assert component["status"] == "healthy"
    assert summary["completed_wait_ms_sum"] == 21_000
    assert summary["completed_wait_record_count"] == 3
    assert summary["pending_wait_record_count"] == 0
    assert summary["by_queue"]["audio"]["completed_wait_ms_sum"] == 5000
    assert summary["by_queue"]["video"]["completed_wait_ms_sum"] == 7000
    assert summary["by_queue"]["publish_job"]["completed_wait_ms_sum"] == 9000
    assert summary["by_language"]["en"]["completed_wait_record_count"] == 1
    assert summary["by_language"]["de"]["completed_wait_ms_sum"] == 7000


def test_publish_job_summary_marks_invalid_manifest_as_operator_attention() -> None:
    episode = Episode.model_construct(
        assets=[
            Asset.model_construct(
                asset_type=AssetType.production_manifest,
                source_entity_type="export_package",
                source_entity_id="package-with-invalid-manifest",
                status="completed",
                generation_metadata={"production_manifest": {"schema_version": "draft"}},
            )
        ],
        publish_jobs=[],
    )
    service = SystemHealthService(Settings())

    summary = service._publish_job_summary([episode])

    assert summary["invalid_production_manifest_assets"] == 1
    assert summary["packages_missing_production_manifest"] == 0
    assert summary["reason"] == "publish jobs need operator attention"


def test_system_health_manifest_validity_requires_delivery_package_chapters() -> None:
    package = Asset.model_construct(
        id="package-chaptered",
        asset_type=AssetType.export_package,
        status="completed",
        source_entity_type="render_asset",
        source_entity_id="render-final",
    )
    manifest = Asset.model_construct(
        asset_type=AssetType.production_manifest,
        source_entity_type="export_package",
        source_entity_id=str(package.id),
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "timeline": {
                    "chapter_count": 1,
                    "chapters": [{"title": "Opening", "start_ms": 0}],
                },
                "delivery_package": {
                    "asset_id": str(package.id),
                    "manifest": {"schema_version": "youtube_package.v1", "chapters": []},
                },
            }
        },
    )
    service = SystemHealthService(Settings())

    validity = service._production_manifest_asset_valid(manifest)

    assert validity == {
        "valid": False,
        "reason": "embedded delivery package chapters do not match timeline chapters",
    }


def test_system_health_manifest_validity_rejects_stale_delivery_package_storage() -> None:
    package = Asset.model_construct(
        id="package-stale-storage",
        asset_type=AssetType.export_package,
        status="completed",
        source_entity_type="render_asset",
        source_entity_id="render-final",
        storage_uri="object://dialecticore/exports/current-package.zip",
        checksum="sha256:current-package",
        generation_metadata={"package_id": "package-current"},
    )
    manifest = Asset.model_construct(
        asset_type=AssetType.production_manifest,
        source_entity_type="export_package",
        source_entity_id=str(package.id),
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {
                    "asset_id": str(package.id),
                    "storage_uri": "object://dialecticore/exports/older-package.zip",
                    "checksum": package.checksum,
                    "package_id": "package-current",
                },
            }
        },
    )
    service = SystemHealthService(Settings())

    validity = service._production_manifest_asset_valid(manifest, package)

    assert validity == {
        "valid": False,
        "reason": "embedded delivery package storage_uri does not match package asset",
    }
