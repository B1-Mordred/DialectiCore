import httpx

from scripts.backup_smoke import (
    backup_readiness_summary,
    backup_summary,
    create_backup,
    listed_backup_summary,
    validate_backup,
    validation_summary,
)


def test_backup_smoke_create_validate_and_summarize_flow() -> None:
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = httpx.Request("POST", "http://test", content=request.content).read()
        decoded = None
        if body:
            import json

            decoded = json.loads(body)
        requests.append((request.method, request.url.path, decoded))
        if request.method == "POST" and request.url.path == "/api/v1/system/backups":
            return httpx.Response(
                200,
                json={
                    "backup_id": "backup-a",
                    "created_at": "2026-07-30T00:00:00+00:00",
                    "archive": {
                        "filename": "backup-a.tar.gz",
                        "checksum": "sha256:abc",
                        "size_bytes": 123,
                    },
                    "database": {"total_records": 4},
                    "object_storage": {"file_count": 2},
                    "runtime_state": {"file_count": 1},
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/system/backups/restore":
            return httpx.Response(
                200,
                json={
                    "status": "validated",
                    "apply": False,
                    "restore_plan": {
                        "schema_version": "backup_restore_plan.v1",
                        "backup_id": "backup-a",
                        "summary": {
                            "target_scope_count": 2,
                            "target_record_count": 0,
                            "target_file_count": 3,
                        },
                        "object_storage": {
                            "will_restore": True,
                            "archive_validation": {
                                "validated": True,
                                "status": "validated",
                                "schema_version": "file_storage_restore_validation.v1",
                                "checksum_verified_count": 2,
                            },
                        },
                        "runtime_state": {
                            "will_restore": True,
                            "archive_validation": {
                                "validated": True,
                                "status": "validated",
                                "schema_version": "file_storage_restore_validation.v1",
                                "checksum_verified_count": 1,
                            },
                        },
                    },
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        created = create_backup(
            client,
            "http://test",
            label="smoke",
            user_id="tester",
            include_object_storage=True,
            include_runtime_state=True,
        )
        validated = validate_backup(
            client,
            "http://test",
            "backup-a.tar.gz",
            user_id="tester",
            restore_object_storage=True,
            restore_runtime_state=True,
        )

    assert requests[0] == (
        "POST",
        "/api/v1/system/backups",
        {
            "label": "smoke",
            "user_id": "tester",
            "include_object_storage": True,
            "include_runtime_state": True,
        },
    )
    assert requests[1] == (
        "POST",
        "/api/v1/system/backups/restore",
        {
            "backup_path": "backup-a.tar.gz",
            "apply": False,
            "restore_database": False,
            "restore_object_storage": True,
            "restore_runtime_state": True,
            "user_id": "tester",
        },
    )
    assert backup_summary(created) == {
        "status": "pass",
        "backup_id": "backup-a",
        "created_at": "2026-07-30T00:00:00+00:00",
        "archive_filename": "backup-a.tar.gz",
        "archive_checksum": "sha256:abc",
        "archive_size_bytes": 123,
        "database_total_records": 4,
        "object_storage_file_count": 2,
        "runtime_state_file_count": 1,
    }
    assert validation_summary(validated) == {
        "status": "validated",
        "apply": False,
        "backup_id": "backup-a",
        "restore_plan_schema_version": "backup_restore_plan.v1",
        "target_scope_count": 2,
        "target_record_count": 0,
        "target_file_count": 3,
        "object_storage": {
            "will_restore": True,
            "validated": True,
            "status": "validated",
            "schema_version": "file_storage_restore_validation.v1",
            "checksum_verified_count": 2,
        },
        "runtime_state": {
            "will_restore": True,
            "validated": True,
            "status": "validated",
            "schema_version": "file_storage_restore_validation.v1",
            "checksum_verified_count": 1,
        },
    }


def test_backup_smoke_summarizes_listing_and_readiness() -> None:
    assert listed_backup_summary(
        {
            "backups": [
                {
                    "backup_id": "backup-a",
                    "restore_validation": {"status": "validated", "validated": True},
                }
            ]
        },
        "backup-a",
    ) == {
        "found": True,
        "backup_id": "backup-a",
        "restore_validation_status": "validated",
        "restore_validation_validated": True,
    }
    assert backup_readiness_summary(
        {
            "checks": [
                {
                    "category": "backup_storage",
                    "status": "pass",
                    "blockers": [],
                    "warnings": [],
                    "details": {
                        "archive_count": 1,
                        "restore_validated_archive_count": 1,
                        "failed_readiness_checks": [],
                        "latest_restore_validation": {"status": "validated"},
                    },
                }
            ]
        }
    ) == {
        "status": "pass",
        "blockers": [],
        "warnings": [],
        "archive_count": 1,
        "restore_validated_archive_count": 1,
        "failed_readiness_checks": [],
        "latest_restore_validation_status": "validated",
    }
