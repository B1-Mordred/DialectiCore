import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from app.core.config import Settings
from app.domain.enums import AssetType
from app.domain.schemas import (
    Asset,
    BackupCreateRequest,
    BackupRestoreRequest,
    EpisodeCreateRequest,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.backup_service import BackupService
from tests.test_discussion_engine import definition


class FakeBackupS3Client:
    def __init__(self) -> None:
        self.bucket_exists = True
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict] = []

    def head_bucket(self, Bucket: str) -> None:
        if not self.bucket_exists:
            raise RuntimeError("404 bucket not found")

    def create_bucket(self, Bucket: str) -> None:
        self.bucket_exists = True

    def list_objects_v2(self, **kwargs: object) -> dict:
        return {
            "Contents": [
                {"Key": key, "Size": len(payload), "ETag": f"etag-{index}"}
                for index, (key, payload) in enumerate(sorted(self.objects.items()))
            ],
            "IsTruncated": False,
        }

    def get_object(self, Bucket: str, Key: str) -> dict:
        return {
            "Body": io.BytesIO(self.objects[Key]),
            "ContentType": "audio/wav" if Key.endswith(".wav") else "application/octet-stream",
        }

    def put_object(self, **kwargs: object) -> None:
        body = kwargs["Body"]
        payload = body.read() if hasattr(body, "read") else body
        assert isinstance(payload, bytes)
        self.objects[str(kwargs["Key"])] = payload
        self.puts.append({**kwargs, "Body": payload})


def rewrite_s3_backup_archive(
    manifest: dict,
    repository: EpisodeRepository,
    object_payloads: dict[str, bytes],
) -> None:
    archive_path = Path(manifest["archive"]["path"])
    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_payload = json.dumps(
            {key: value for key, value in manifest.items() if key != "archive"},
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))

        database_payload = json.dumps(repository.export_backup_data()).encode("utf-8")
        database_info = tarfile.TarInfo("database.json")
        database_info.size = len(database_payload)
        archive.addfile(database_info, io.BytesIO(database_payload))

        for key, payload in object_payloads.items():
            object_info = tarfile.TarInfo(f"object-storage-s3/{key}")
            object_info.size = len(payload)
            archive.addfile(object_info, io.BytesIO(payload))


def rewrite_file_backup_archive(
    manifest: dict,
    repository: EpisodeRepository,
    object_payloads: dict[str, bytes],
    runtime_payloads: dict[str, bytes] | None = None,
) -> None:
    archive_path = Path(manifest["archive"]["path"])
    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_payload = json.dumps(
            {key: value for key, value in manifest.items() if key != "archive"},
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))

        database_payload = json.dumps(repository.export_backup_data()).encode("utf-8")
        database_info = tarfile.TarInfo("database.json")
        database_info.size = len(database_payload)
        archive.addfile(database_info, io.BytesIO(database_payload))

        for key, payload in object_payloads.items():
            object_info = tarfile.TarInfo(f"object-storage/{key}")
            object_info.size = len(payload)
            archive.addfile(object_info, io.BytesIO(payload))

        for key, payload in (runtime_payloads or {}).items():
            runtime_info = tarfile.TarInfo(f"runtime-state/{key}")
            runtime_info.size = len(payload)
            archive.addfile(runtime_info, io.BytesIO(payload))


def test_backup_service_creates_archive_and_restores_database_and_files(tmp_path: Path) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language="en",
            source_entity_type="transcript_turn",
            source_entity_id="turn-1",
            storage_uri="object://dialecticore/audio/sample.wav",
            mime_type="audio/wav",
            duration_ms=1000,
            checksum="sha256:audio",
            status="completed",
        )
    )
    repository.save(episode)
    object_file = (
        Path(settings.object_storage_local_path)
        / settings.object_storage_bucket
        / "audio"
        / "sample.wav"
    )
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(b"audio")
    runtime_file = Path(settings.runtime_state_path) / "workers" / "worker.json"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text('{"role":"voicebox-adapter"}', encoding="utf-8")
    service = BackupService(settings)

    manifest = service.create_backup(
        repository,
        BackupCreateRequest(label="nightly", user_id="tester"),
    )

    assert manifest["backup_id"].endswith("-nightly")
    assert manifest["database"]["record_counts"]["episode_records"] == 1
    assert manifest["database"]["record_counts"]["asset_records"] == 1
    assert manifest["object_storage"]["file_count"] == 1
    assert manifest["runtime_state"]["file_count"] == 1
    assert manifest["archive"]["checksum"].startswith("sha256:")
    assert Path(manifest["archive"]["path"]).exists()
    assert repository.list_audit_events(limit=1)[0].event_type == "backup.created"

    object_file.unlink()
    runtime_file.unlink()
    target_repository = EpisodeRepository()
    restored = service.restore_backup(
        target_repository,
        BackupRestoreRequest(
            backup_path=manifest["archive"]["filename"],
            apply=True,
            restore_runtime_state=True,
            user_id="restore-user",
        ),
    )

    assert restored["status"] == "restored"
    assert restored["restored"]["database"]["record_counts"]["episode_records"] == 1
    assert restored["restored"]["database"]["record_counts"]["asset_records"] == 1
    assert restored["restored"]["object_storage"]["file_count"] == 1
    assert restored["restored"]["runtime_state"]["file_count"] == 1
    assert target_repository.get(episode.id).title == episode.title
    restored_assets = target_repository.list_assets(episode.id)
    assert len(restored_assets) == 1
    assert restored_assets[0].asset_type == AssetType.audio
    assert object_file.read_bytes() == b"audio"
    assert runtime_file.read_text(encoding="utf-8") == '{"role":"voicebox-adapter"}'
    assert target_repository.list_audit_events(limit=1)[0].event_type == "backup.restored"


def test_backup_service_dry_run_validates_local_file_archive_integrity(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    object_file = (
        Path(settings.object_storage_local_path)
        / settings.object_storage_bucket
        / "audio"
        / "sample.wav"
    )
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(b"audio")
    runtime_file = Path(settings.runtime_state_path) / "workers" / "worker.json"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text('{"role":"voicebox-adapter"}', encoding="utf-8")
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="local-validate"))

    result = service.restore_backup(
        repository,
        BackupRestoreRequest(
            backup_path=manifest["archive"]["filename"],
            apply=False,
            restore_object_storage=True,
            restore_runtime_state=True,
            restore_database=False,
        ),
    )

    assert result["status"] == "validated"
    assert result["restore_plan"]["object_storage"]["archive_validation"] == {
        "schema_version": "file_storage_restore_validation.v1",
        "validated": True,
        "prefix": "object-storage",
        "expected_file_count": 1,
        "archive_file_count": 1,
        "total_bytes": len(b"audio"),
        "size_verified_count": 1,
        "checksum_verified_count": 1,
    }
    assert result["restore_plan"]["runtime_state"]["archive_validation"] == {
        "schema_version": "file_storage_restore_validation.v1",
        "validated": True,
        "prefix": "runtime-state",
        "expected_file_count": 1,
        "archive_file_count": 1,
        "total_bytes": len(b'{"role":"voicebox-adapter"}'),
        "size_verified_count": 1,
        "checksum_verified_count": 1,
    }
    audit = repository.list_audit_events(limit=1)[0]
    assert audit.event_type == "backup.restore_validated"
    assert (
        audit.details["restore_plan"]["runtime_state"]["archive_validation"][
            "schema_version"
        ]
        == "file_storage_restore_validation.v1"
    )


def test_backup_service_runtime_snapshot_survives_live_file_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    runtime_file = Path(settings.runtime_state_path) / "worker-leases" / "render.json"
    runtime_file.parent.mkdir(parents=True)
    initial_payload = b'{"owner":"render-1","epoch":1}'
    rewritten_payload = b'{"owner":"render-2","epoch":2}'
    runtime_file.write_bytes(initial_payload)
    repository = EpisodeRepository()
    service = BackupService(settings)
    original_add_files = service._add_files

    def rewrite_live_state_before_archive(**kwargs: object) -> None:
        if kwargs["prefix"] == "runtime-state":
            runtime_file.write_bytes(rewritten_payload)
        original_add_files(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_add_files", rewrite_live_state_before_archive)
    manifest = service.create_backup(
        repository,
        BackupCreateRequest(label="runtime-rewrite"),
    )

    validated = service.restore_backup(
        repository,
        BackupRestoreRequest(
            backup_path=manifest["archive"]["filename"],
            apply=False,
            restore_database=False,
            restore_object_storage=False,
            restore_runtime_state=True,
        ),
    )

    assert runtime_file.read_bytes() == rewritten_payload
    assert manifest["runtime_state"]["root"] == str(Path(settings.runtime_state_path))
    assert manifest["runtime_state"]["files"] == [
        {
            "path": "worker-leases/render.json",
            "size_bytes": len(initial_payload),
            "checksum": "sha256:" + hashlib.sha256(initial_payload).hexdigest(),
        }
    ]
    assert validated["status"] == "validated"
    assert validated["restore_plan"]["runtime_state"]["archive_validation"][
        "checksum_verified_count"
    ] == 1


def test_backup_service_dry_run_rejects_tampered_local_object_file(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    object_file = (
        Path(settings.object_storage_local_path)
        / settings.object_storage_bucket
        / "audio"
        / "sample.wav"
    )
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(b"audio")
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="local-tamper"))
    rewrite_file_backup_archive(manifest, repository, {"dialecticore/audio/sample.wav": b"Audio"})

    with pytest.raises(ValueError, match="checksum mismatch"):
        service.restore_backup(
            repository,
            BackupRestoreRequest(
                backup_path=manifest["archive"]["filename"],
                apply=False,
                restore_object_storage=True,
                restore_runtime_state=False,
                restore_database=False,
            ),
        )


def test_backup_service_validates_restore_without_applying(tmp_path: Path) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="dry-run"))

    result = service.restore_backup(
        repository,
        BackupRestoreRequest(backup_path=manifest["archive"]["filename"], apply=False),
    )
    expected_record_count = manifest["database"]["total_records"]

    assert result["status"] == "validated"
    assert result["apply"] is False
    assert result["restore_plan"]["schema_version"] == "backup_restore_plan.v1"
    assert result["restore_plan"]["database"]["will_restore"] is True
    assert result["restore_plan"]["database"]["total_records"] == expected_record_count
    assert result["restore_plan"]["object_storage"]["will_restore"] is False
    assert result["restore_plan"]["runtime_state"]["will_restore"] is False
    assert result["restore_plan"]["summary"]["target_scope_count"] == 1
    assert result["restore_plan"]["summary"]["target_record_count"] == expected_record_count
    assert result["restore_plan"]["summary"]["target_file_count"] == 0
    assert result["restored"] == {}
    audit = repository.list_audit_events(limit=1)[0]
    assert audit.event_type == "backup.restore_validated"
    assert audit.details["restore_plan"]["schema_version"] == "backup_restore_plan.v1"


def test_backup_service_validation_requires_current_archive_checksum(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="checksum"))
    service.restore_backup(
        repository,
        BackupRestoreRequest(backup_path=manifest["archive"]["filename"], apply=False),
    )
    validation_event = repository.list_audit_events(limit=1)[0]
    archive_path = Path(manifest["archive"]["path"])

    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_payload = json.dumps(
            {key: value for key, value in manifest.items() if key != "archive"},
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))
        database_payload = json.dumps(repository.export_backup_data()).encode("utf-8")
        database_info = tarfile.TarInfo("database.json")
        database_info.size = len(database_payload)
        archive.addfile(database_info, io.BytesIO(database_payload))
        tamper_payload = b"archive changed after restore validation"
        tamper_info = tarfile.TarInfo("tamper.txt")
        tamper_info.size = len(tamper_payload)
        archive.addfile(tamper_info, io.BytesIO(tamper_payload))

    listed = service.list_backups([validation_event])

    assert listed[0]["restore_validation"]["validated"] is False
    assert listed[0]["restore_validation"]["status"] == "checksum_mismatch"
    assert listed[0]["restore_validation"]["archive_checksum"] == listed[0]["archive"][
        "checksum"
    ]
    assert (
        listed[0]["restore_validation"]["validated_archive_checksum"]
        == validation_event.details["archive_checksum"]
    )


def test_backup_service_listing_skips_large_archive_checksum_without_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="large-list"))
    service.restore_backup(
        repository,
        BackupRestoreRequest(backup_path=manifest["archive"]["filename"], apply=False),
    )
    validation_event = repository.list_audit_events(limit=1)[0]
    import app.services.backup_service as backup_module

    monkeypatch.setattr(backup_module, "BACKUP_LIST_CHECKSUM_MAX_BYTES", 1)

    listed = service.list_backups([validation_event])

    assert listed[0]["archive"]["checksum"] is None
    assert listed[0]["archive"]["checksum_status"] == "skipped"
    assert listed[0]["restore_validation"] == {
        "validated": False,
        "status": "checksum_not_evaluated",
        "reason": (
            "backup archive checksum was not evaluated because the archive "
            "exceeds the listing checksum limit"
        ),
        "backup_id": manifest["backup_id"],
        "validated_archive_checksum": validation_event.details["archive_checksum"],
    }


def test_backup_service_skips_archives_missing_manifest(tmp_path: Path) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    backup_root = Path(settings.backup_path)
    backup_root.mkdir(parents=True)
    archive_path = backup_root / "dialecticore-backup-broken.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = json.dumps({"schema_version": "dialecticore.backup.v1"}).encode("utf-8")
        info = tarfile.TarInfo("database.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    service = BackupService(settings)

    assert service.list_backups() == []
    with pytest.raises(ValueError, match="missing manifest.json"):
        service.inspect_backup(archive_path.name)


def test_backup_service_restore_rejects_archive_missing_database_payload(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="broken"))
    archive_path = Path(manifest["archive"]["path"])

    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_payload = json.dumps(
            {key: value for key, value in manifest.items() if key != "archive"},
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))

    with pytest.raises(ValueError, match="missing database.json"):
        service.restore_backup(
            repository,
            BackupRestoreRequest(backup_path=manifest["archive"]["filename"], apply=False),
        )


def test_backup_service_rejects_duplicate_required_json_members(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="duplicate-json"))
    archive_path = Path(manifest["archive"]["path"])

    with tarfile.open(archive_path, "w:gz") as archive:
        for label in ("first", "second"):
            manifest_payload = json.dumps(
                {"schema_version": "dialecticore.backup.v1", "backup_id": label}
            ).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_payload)
            archive.addfile(manifest_info, io.BytesIO(manifest_payload))
        database_payload = json.dumps(repository.export_backup_data()).encode("utf-8")
        database_info = tarfile.TarInfo("database.json")
        database_info.size = len(database_payload)
        archive.addfile(database_info, io.BytesIO(database_payload))

    assert service.list_backups() == []
    with pytest.raises(ValueError, match="multiple manifest.json"):
        service.inspect_backup(archive_path.name)


def test_backup_service_rejects_required_json_link_members(tmp_path: Path) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository = EpisodeRepository()
    service = BackupService(settings)
    manifest = service.create_backup(repository, BackupCreateRequest(label="linked-json"))
    archive_path = Path(manifest["archive"]["path"])

    with tarfile.open(archive_path, "w:gz") as archive:
        target_payload = json.dumps(
            {key: value for key, value in manifest.items() if key != "archive"}
        ).encode("utf-8")
        target_info = tarfile.TarInfo("linked-manifest.json")
        target_info.size = len(target_payload)
        archive.addfile(target_info, io.BytesIO(target_payload))
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.type = tarfile.SYMTYPE
        manifest_info.linkname = "linked-manifest.json"
        archive.addfile(manifest_info)
        database_payload = json.dumps(repository.export_backup_data()).encode("utf-8")
        database_info = tarfile.TarInfo("database.json")
        database_info.size = len(database_payload)
        archive.addfile(database_info, io.BytesIO(database_payload))

    assert service.list_backups() == []
    with pytest.raises(ValueError, match="manifest.json must be a regular file"):
        service.inspect_backup(archive_path.name)


def test_backup_service_archives_and_restores_remote_s3_bucket(tmp_path: Path) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_backend="s3",
        object_storage_endpoint=(
            "https://minio-user:leaked-minio-secret@minio.local:9000/root"
            "?token=leaked-query-token"
        ),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    client = FakeBackupS3Client()
    client.objects = {
        "audio/turn.wav": b"audio",
        "renders/final.mp4": b"video",
    }
    repository = EpisodeRepository()
    service = BackupService(settings, s3_client=client)

    manifest = service.create_backup(
        repository,
        BackupCreateRequest(label="s3-full", user_id="tester"),
    )

    assert manifest["object_storage"]["backend"] == "s3"
    assert manifest["object_storage"]["source"] == "s3_bucket"
    assert manifest["object_storage"]["archive_prefix"] == "object-storage-s3"
    assert manifest["object_storage"]["endpoint"] == "https://minio.local:9000/root"
    assert manifest["object_storage"]["file_count"] == 2
    assert manifest["object_storage"]["total_bytes"] == len(b"audio") + len(b"video")
    assert [item["key"] for item in manifest["object_storage"]["objects"]] == [
        "audio/turn.wav",
        "renders/final.mp4",
    ]
    inspected = service.inspect_backup(manifest["archive"]["filename"])
    assert inspected["object_storage"]["endpoint"] == "https://minio.local:9000/root"
    manifest_json = json.dumps(inspected, sort_keys=True)
    assert "leaked-minio-secret" not in manifest_json
    assert "leaked-query-token" not in manifest_json

    client.objects = {}
    restored = service.restore_backup(
        repository,
        BackupRestoreRequest(
            backup_path=manifest["archive"]["filename"],
            apply=True,
            restore_database=False,
            restore_object_storage=True,
            restore_runtime_state=False,
            user_id="restore-user",
        ),
    )

    assert restored["status"] == "restored"
    assert restored["restored"]["object_storage"] == {
        "file_count": 2,
        "total_bytes": len(b"audio") + len(b"video"),
        "backend": "s3",
        "bucket": "dialecticore",
        "source": "s3_bucket",
        "manifest_object_count": 2,
        "size_verified_count": 2,
        "checksum_verified_count": 2,
    }
    assert client.objects == {
        "audio/turn.wav": b"audio",
        "renders/final.mp4": b"video",
    }
    assert [put["Key"] for put in client.puts] == ["audio/turn.wav", "renders/final.mp4"]
    assert [put["ContentType"] for put in client.puts] == [
        "audio/wav",
        "application/octet-stream",
    ]


def test_backup_service_dry_run_validates_s3_object_archive_integrity(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_backend="s3",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    client = FakeBackupS3Client()
    client.objects = {
        "audio/turn.wav": b"audio",
        "renders/final.mp4": b"video",
    }
    repository = EpisodeRepository()
    service = BackupService(settings, s3_client=client)
    manifest = service.create_backup(repository, BackupCreateRequest(label="s3-validate"))

    result = service.restore_backup(
        repository,
        BackupRestoreRequest(
            backup_path=manifest["archive"]["filename"],
            apply=False,
            restore_database=False,
            restore_object_storage=True,
            restore_runtime_state=False,
            user_id="restore-user",
        ),
    )

    assert result["status"] == "validated"
    assert result["restore_plan"]["object_storage"]["will_restore"] is True
    assert result["restore_plan"]["object_storage"]["archive_validation"] == {
        "schema_version": "s3_object_storage_restore_validation.v1",
        "validated": True,
        "expected_object_count": 2,
        "archive_object_count": 2,
        "total_bytes": len(b"audio") + len(b"video"),
        "size_verified_count": 2,
        "checksum_verified_count": 2,
    }
    assert result["restored"] == {}
    assert client.puts == []
    audit = repository.list_audit_events(limit=1)[0]
    assert audit.event_type == "backup.restore_validated"
    assert (
        audit.details["restore_plan"]["object_storage"]["archive_validation"][
            "schema_version"
        ]
        == "s3_object_storage_restore_validation.v1"
    )


def test_backup_service_dry_run_rejects_tampered_s3_object_archive(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_backend="s3",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    client = FakeBackupS3Client()
    client.objects = {"audio/turn.wav": b"audio"}
    repository = EpisodeRepository()
    service = BackupService(settings, s3_client=client)
    manifest = service.create_backup(repository, BackupCreateRequest(label="s3-dry-tamper"))
    rewrite_s3_backup_archive(manifest, repository, {"audio/turn.wav": b"Audio"})
    client.objects = {}

    with pytest.raises(ValueError, match="checksum mismatch"):
        service.restore_backup(
            repository,
            BackupRestoreRequest(
                backup_path=manifest["archive"]["filename"],
                apply=False,
                restore_database=False,
                restore_object_storage=True,
                restore_runtime_state=False,
            ),
        )

    assert client.puts == []


def test_backup_service_dry_run_rejects_unexpected_s3_archive_object(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_backend="s3",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    client = FakeBackupS3Client()
    client.objects = {"audio/turn.wav": b"audio"}
    repository = EpisodeRepository()
    service = BackupService(settings, s3_client=client)
    manifest = service.create_backup(repository, BackupCreateRequest(label="s3-extra"))
    rewrite_s3_backup_archive(
        manifest,
        repository,
        {
            "audio/turn.wav": b"audio",
            "unexpected/payload.bin": b"extra",
        },
    )
    client.objects = {}

    with pytest.raises(ValueError, match="not listed in manifest"):
        service.restore_backup(
            repository,
            BackupRestoreRequest(
                backup_path=manifest["archive"]["filename"],
                apply=False,
                restore_database=False,
                restore_object_storage=True,
                restore_runtime_state=False,
            ),
        )

    assert client.puts == []


def test_backup_service_rejects_tampered_s3_object_payload(tmp_path: Path) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_backend="s3",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    client = FakeBackupS3Client()
    client.objects = {"audio/turn.wav": b"audio"}
    repository = EpisodeRepository()
    service = BackupService(settings, s3_client=client)
    manifest = service.create_backup(repository, BackupCreateRequest(label="s3-tamper"))
    rewrite_s3_backup_archive(manifest, repository, {"audio/turn.wav": b"Audio"})
    client.objects = {}

    with pytest.raises(ValueError, match="checksum mismatch"):
        service.restore_backup(
            repository,
            BackupRestoreRequest(
                backup_path=manifest["archive"]["filename"],
                apply=True,
                restore_database=False,
                restore_object_storage=True,
                restore_runtime_state=False,
            ),
        )

    assert client.puts == []


def test_backup_service_rejects_s3_archive_missing_manifest_object(
    tmp_path: Path,
) -> None:
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_backend="s3",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    client = FakeBackupS3Client()
    client.objects = {
        "audio/turn.wav": b"audio",
        "renders/final.mp4": b"video",
    }
    repository = EpisodeRepository()
    service = BackupService(settings, s3_client=client)
    manifest = service.create_backup(repository, BackupCreateRequest(label="s3-missing"))
    rewrite_s3_backup_archive(manifest, repository, {"audio/turn.wav": b"audio"})
    client.objects = {}

    with pytest.raises(ValueError, match="missing from archive"):
        service.restore_backup(
            repository,
            BackupRestoreRequest(
                backup_path=manifest["archive"]["filename"],
                apply=True,
                restore_database=False,
                restore_object_storage=True,
                restore_runtime_state=False,
            ),
        )

    assert client.puts == []
