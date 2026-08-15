from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import tarfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.core.config import Settings
from app.domain.schemas import AuditEvent, BackupCreateRequest, BackupRestoreRequest
from app.services.object_storage import S3ObjectStore

BACKUP_LIST_CHECKSUM_MAX_BYTES = 64 * 1024 * 1024


class BackupRepository(Protocol):
    def export_backup_data(self) -> dict: ...

    def restore_backup_data(self, backup_data: dict, replace_existing: bool = True) -> dict: ...

    def record_global_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    def list_audit_events(
        self,
        limit: int = 50,
        event_type: str | None = None,
    ) -> list[AuditEvent]: ...


class BackupService:
    def __init__(self, settings: Settings, s3_client: object | None = None) -> None:
        self.settings = settings
        self.backup_root = Path(settings.backup_path).expanduser()
        self._s3_client = s3_client

    def create_backup(self, repository: BackupRepository, request: BackupCreateRequest) -> dict:
        created_at = datetime.now(UTC)
        backup_id = self._backup_id(created_at, request.label)
        archive_path = self.backup_root / f"{backup_id}.tar.gz"
        self.backup_root.mkdir(parents=True, exist_ok=True)

        database_payload = repository.export_backup_data()
        with TemporaryDirectory() as temp_name:
            temp_root = Path(temp_name)
            database_path = temp_root / "database.json"
            database_path.write_text(
                json.dumps(database_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            object_storage_files = (
                self._prepare_object_storage_files(temp_root)
                if request.include_object_storage
                else self._empty_object_storage_files()
            )
            runtime_state_root = Path(self.settings.runtime_state_path).expanduser()
            runtime_state_snapshot = (
                self._snapshot_file_tree(
                    source_root=runtime_state_root,
                    snapshot_root=temp_root / "runtime-state",
                )
                if request.include_runtime_state
                else {"root": temp_root / "runtime-state", "files": []}
            )
            manifest = {
                "schema_version": "dialecticore.backup.v1",
                "backup_id": backup_id,
                "created_at": created_at.isoformat(),
                "label": request.label,
                "created_by": request.user_id or "system",
                "database": {
                    "schema_version": database_payload["schema_version"],
                    "record_counts": database_payload["record_counts"],
                    "total_records": sum(database_payload["record_counts"].values()),
                },
                "object_storage": self._object_storage_section(
                    prepared=object_storage_files,
                    requested=request.include_object_storage,
                ),
                "runtime_state": self._file_section(
                    root=runtime_state_snapshot["root"],
                    files=runtime_state_snapshot["files"],
                    requested=request.include_runtime_state,
                    backend="filesystem",
                    bucket=None,
                )
                | {"root": str(runtime_state_root)},
            }
            manifest_path = temp_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(manifest_path, arcname="manifest.json")
                archive.add(database_path, arcname="database.json")
                self._add_files(
                    archive=archive,
                    root=object_storage_files["root"],
                    prefix=object_storage_files["archive_prefix"],
                    files=object_storage_files["files"],
                )
                self._add_files(
                    archive=archive,
                    root=runtime_state_snapshot["root"],
                    prefix="runtime-state",
                    files=runtime_state_snapshot["files"],
                )

        enriched = self._enrich_manifest(manifest, archive_path)
        repository.record_global_audit_event(
            AuditEvent(
                event_type="backup.created",
                actor=request.user_id or "system",
                details={
                    "backup_id": backup_id,
                    "archive_path": str(archive_path),
                    "archive_checksum": enriched["archive"]["checksum"],
                    "database_total_records": manifest["database"]["total_records"],
                    "object_storage_file_count": manifest["object_storage"]["file_count"],
                    "object_storage_source": manifest["object_storage"]["source"],
                    "runtime_state_file_count": manifest["runtime_state"]["file_count"],
                },
            )
        )
        return enriched

    def list_backups(self, restore_validation_events: list[AuditEvent] | None = None) -> list[dict]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        manifests = []
        for path in sorted(self.backup_root.glob("*.tar.gz"), reverse=True):
            try:
                manifest = self._inspect_backup_for_listing(path)
                manifests.append(
                    manifest
                    | {
                        "restore_validation": self._backup_restore_validation(
                            manifest,
                            path,
                            restore_validation_events or [],
                        )
                    }
                )
            except (OSError, tarfile.TarError, ValueError, json.JSONDecodeError):
                continue
        return manifests

    def inspect_backup(self, backup_path: str) -> dict:
        archive_path = self._resolve_backup_path(backup_path)
        manifest = self._read_archive_json(archive_path, "manifest.json")
        return self._enrich_manifest(manifest, archive_path)

    def _inspect_backup_for_listing(self, archive_path: Path) -> dict:
        size_bytes = archive_path.stat().st_size
        if size_bytes <= BACKUP_LIST_CHECKSUM_MAX_BYTES:
            manifest = self._read_archive_json(archive_path, "manifest.json")
        else:
            try:
                manifest = self._read_archive_json_fast(archive_path, "manifest.json")
            except (OSError, tarfile.TarError, ValueError, json.JSONDecodeError):
                raise
        return manifest | {
            "archive": self._archive_info(
                archive_path,
                checksum_max_bytes=BACKUP_LIST_CHECKSUM_MAX_BYTES,
            )
        }

    def restore_backup(self, repository: BackupRepository, request: BackupRestoreRequest) -> dict:
        archive_path = self._resolve_backup_path(request.backup_path)
        manifest = self._read_archive_json(archive_path, "manifest.json")
        database_payload = self._read_archive_json(archive_path, "database.json")
        object_storage_validation = self._validate_object_storage_restore_archive(
            archive_path,
            manifest.get("object_storage", {}),
            request,
        )
        runtime_state_validation = self._validate_runtime_state_restore_archive(
            archive_path,
            manifest.get("runtime_state", {}),
            request,
        )
        restore_plan = self._restore_plan(
            manifest,
            database_payload,
            request,
            object_storage_validation=object_storage_validation,
            runtime_state_validation=runtime_state_validation,
        )
        result = {
            "status": "validated",
            "apply": request.apply,
            "archive": self._archive_info(archive_path),
            "manifest": manifest,
            "database": {
                "schema_version": database_payload.get("schema_version"),
                "record_counts": database_payload.get("record_counts", {}),
            },
            "restore_plan": restore_plan,
            "restored": {},
        }
        if not request.apply:
            repository.record_global_audit_event(
                AuditEvent(
                    event_type="backup.restore_validated",
                    actor=request.user_id or "system",
                    details={
                        "backup_id": manifest.get("backup_id"),
                        "archive_path": str(archive_path),
                        "archive_checksum": result["archive"]["checksum"],
                        "restore_plan": restore_plan,
                    },
                )
            )
            return result

        if request.restore_database:
            result["restored"]["database"] = repository.restore_backup_data(
                database_payload,
                replace_existing=request.replace_existing,
            )
        if request.restore_object_storage:
            result["restored"]["object_storage"] = self._restore_object_storage(
                archive_path,
                manifest.get("object_storage", {}),
            )
        if request.restore_runtime_state:
            result["restored"]["runtime_state"] = self._extract_prefix(
                archive_path,
                "runtime-state",
                Path(self.settings.runtime_state_path).expanduser(),
            )
        result["status"] = "restored"
        repository.record_global_audit_event(
            AuditEvent(
                event_type="backup.restored",
                actor=request.user_id or "system",
                details={
                    "backup_id": manifest.get("backup_id"),
                    "archive_path": str(archive_path),
                    "restore_database": request.restore_database,
                    "restore_object_storage": request.restore_object_storage,
                    "restore_runtime_state": request.restore_runtime_state,
                    "replace_existing": request.replace_existing,
                    "restore_plan": restore_plan,
                    "restored": result["restored"],
                },
            )
        )
        return result

    def _restore_plan(
        self,
        manifest: dict,
        database_payload: dict,
        request: BackupRestoreRequest,
        object_storage_validation: dict | None = None,
        runtime_state_validation: dict | None = None,
    ) -> dict:
        database_counts = database_payload.get("record_counts", {})
        if not isinstance(database_counts, dict):
            database_counts = {}
        object_storage = manifest.get("object_storage") or {}
        runtime_state = manifest.get("runtime_state") or {}
        database_total_records = sum(
            int(value)
            for value in database_counts.values()
            if isinstance(value, int)
        )
        object_file_count = int(object_storage.get("file_count") or 0)
        runtime_file_count = int(runtime_state.get("file_count") or 0)
        will_restore_database = request.restore_database
        will_restore_object_storage = request.restore_object_storage and bool(
            object_storage.get("included")
        )
        will_restore_runtime_state = request.restore_runtime_state and bool(
            runtime_state.get("included")
        )
        return {
            "schema_version": "backup_restore_plan.v1",
            "apply": request.apply,
            "replace_existing": request.replace_existing,
            "backup_id": manifest.get("backup_id"),
            "database": {
                "requested": request.restore_database,
                "will_restore": will_restore_database,
                "record_counts": database_counts,
                "total_records": database_total_records,
            },
            "object_storage": {
                "requested": request.restore_object_storage,
                "included": bool(object_storage.get("included")),
                "will_restore": will_restore_object_storage,
                "backend": object_storage.get("backend"),
                "source": object_storage.get("source"),
                "file_count": object_file_count,
                "total_bytes": int(object_storage.get("total_bytes") or 0),
                "archive_validation": object_storage_validation,
            },
            "runtime_state": {
                "requested": request.restore_runtime_state,
                "included": bool(runtime_state.get("included")),
                "will_restore": will_restore_runtime_state,
                "file_count": runtime_file_count,
                "total_bytes": int(runtime_state.get("total_bytes") or 0),
                "archive_validation": runtime_state_validation,
            },
            "summary": {
                "target_scope_count": sum(
                    1
                    for enabled in (
                        will_restore_database,
                        will_restore_object_storage,
                        will_restore_runtime_state,
                    )
                    if enabled
                ),
                "target_record_count": database_total_records
                if will_restore_database
                else 0,
                "target_file_count": (
                    object_file_count if will_restore_object_storage else 0
                )
                + (runtime_file_count if will_restore_runtime_state else 0),
            },
        }

    def _backup_id(self, created_at: datetime, label: str | None) -> str:
        timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
        suffix = self._slug(label or "manual")
        return f"dialecticore-backup-{timestamp}-{suffix}"

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
        return (slug or "manual")[:80]

    def _iter_files(self, root: Path) -> Iterable[Path]:
        if not root.exists():
            return []
        return sorted(path for path in root.rglob("*") if path.is_file())

    def _prepare_object_storage_files(self, temp_root: Path) -> dict:
        if self._is_s3_backend():
            return self._prepare_s3_object_storage_files(temp_root)
        root = Path(self.settings.object_storage_local_path).expanduser()
        return {
            "root": root,
            "files": list(self._iter_files(root)),
            "archive_prefix": "object-storage",
            "source": "local_filesystem",
            "objects": [],
        }

    def _empty_object_storage_files(self) -> dict:
        return {
            "root": Path(self.settings.object_storage_local_path).expanduser(),
            "files": [],
            "archive_prefix": "object-storage",
            "source": "not_requested",
            "objects": [],
        }

    def _snapshot_file_tree(self, source_root: Path, snapshot_root: Path) -> dict:
        """Capture a mutable, small filesystem tree for a consistent archive.

        Runtime worker state is rewritten while a backup is being assembled.  The
        manifest and tar archive must therefore consume the same captured bytes,
        rather than opening each live file independently at different times.
        Files that disappear between discovery and capture are omitted; any other
        read error fails the backup instead of publishing an unverifiable archive.
        """
        snapshot_root.mkdir(parents=True, exist_ok=True)
        captured_files: list[Path] = []
        for source_path in self._iter_files(source_root):
            relative_path = self._safe_relative_archive_path(
                source_path.relative_to(source_root).as_posix()
            )
            try:
                payload = source_path.read_bytes()
            except FileNotFoundError:
                continue
            target_path = snapshot_root / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payload)
            captured_files.append(target_path)
        return {"root": snapshot_root, "files": captured_files}

    def _prepare_s3_object_storage_files(self, temp_root: Path) -> dict:
        root = temp_root / "object-storage-s3"
        root.mkdir(parents=True, exist_ok=True)
        store = self._s3_store()
        store._ensure_bucket()
        client = store.client
        objects = []
        files = []
        for item in self._list_s3_objects(client):
            key = self._safe_s3_key(str(item["Key"]))
            response = client.get_object(Bucket=self.settings.object_storage_bucket, Key=key)
            body = response.get("Body", b"")
            payload = body.read() if hasattr(body, "read") else body
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            if not isinstance(payload, bytes):
                payload = bytes(payload)
            target = root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
            objects.append(
                {
                    "key": key,
                    "size_bytes": len(payload),
                    "checksum": checksum,
                    "content_type": response.get(
                        "ContentType",
                        mimetypes.guess_type(key)[0] or "application/octet-stream",
                    ),
                    "etag": item.get("ETag"),
                }
            )
            files.append(target)
        return {
            "root": root,
            "files": files,
            "archive_prefix": "object-storage-s3",
            "source": "s3_bucket",
            "objects": objects,
        }

    def _file_section(
        self,
        root: Path,
        files: list[Path],
        requested: bool,
        backend: str,
        bucket: str | None,
    ) -> dict:
        total_bytes = sum(path.stat().st_size for path in files)
        return {
            "requested": requested,
            "included": requested and root.exists(),
            "backend": backend,
            "bucket": bucket,
            "source": "local_filesystem",
            "archive_prefix": None,
            "root": str(root),
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": self._file_manifest_entries(root, files),
        }

    def _object_storage_section(self, prepared: dict, requested: bool) -> dict:
        files = prepared["files"]
        total_bytes = sum(path.stat().st_size for path in files)
        section = {
            "requested": requested,
            "included": requested and (bool(files) or prepared["root"].exists()),
            "backend": self.settings.object_storage_backend,
            "bucket": self.settings.object_storage_bucket,
            "source": prepared["source"],
            "archive_prefix": prepared["archive_prefix"],
            "root": str(prepared["root"]),
            "file_count": len(files),
            "total_bytes": total_bytes,
        }
        if not prepared["objects"]:
            section["files"] = self._file_manifest_entries(prepared["root"], files)
        if prepared["objects"]:
            section["objects"] = prepared["objects"]
        if self._is_s3_backend():
            section["endpoint"] = self._safe_endpoint_metadata(
                self.settings.object_storage_endpoint
            )
            section["region"] = self.settings.object_storage_region
            section["local_probe_cache_root"] = str(
                Path(self.settings.object_storage_local_path).expanduser()
            )
        return section

    def _add_files(
        self,
        archive: tarfile.TarFile,
        root: Path,
        prefix: str,
        files: list[Path],
    ) -> None:
        if not root.exists():
            return
        for path in files:
            archive.add(path, arcname=f"{prefix}/{path.relative_to(root).as_posix()}")

    def _read_archive_json(self, archive_path: Path, name: str) -> dict:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = self._required_archive_json_member(archive, name)
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"backup archive is missing {name}")
            return json.loads(handle.read().decode("utf-8"))

    def _read_archive_json_fast(self, archive_path: Path, name: str) -> dict:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = archive.next()
            if member is None or member.name != name:
                raise ValueError(f"backup archive first member is not {name}")
            if not member.isfile():
                raise ValueError(f"backup archive {name} must be a regular file")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"backup archive is missing {name}")
            return json.loads(handle.read().decode("utf-8"))

    def _required_archive_json_member(
        self,
        archive: tarfile.TarFile,
        name: str,
    ) -> tarfile.TarInfo:
        members = [member for member in archive.getmembers() if member.name == name]
        if not members:
            raise ValueError(f"backup archive is missing {name}")
        if len(members) > 1:
            raise ValueError(f"backup archive contains multiple {name} members")
        member = members[0]
        if not member.isfile():
            raise ValueError(f"backup archive {name} must be a regular file")
        return member

    def _extract_prefix(self, archive_path: Path, prefix: str, target_root: Path) -> dict:
        target_root.mkdir(parents=True, exist_ok=True)
        extracted_count = 0
        extracted_bytes = 0
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith(f"{prefix}/"):
                    continue
                relative = Path(member.name).relative_to(prefix)
                target = (target_root / relative).resolve()
                if not self._is_relative_to(target, target_root.resolve()):
                    raise ValueError(f"unsafe backup archive path: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                target.write_bytes(data)
                extracted_count += 1
                extracted_bytes += len(data)
        return {"file_count": extracted_count, "total_bytes": extracted_bytes}

    def _restore_object_storage(self, archive_path: Path, section: dict) -> dict:
        if self._is_s3_backend() and section.get("archive_prefix") == "object-storage-s3":
            validation = self._validate_s3_object_storage_archive(
                archive_path,
                "object-storage-s3",
                section,
            )
            return self._restore_s3_object_storage(
                archive_path,
                "object-storage-s3",
                section,
                validation,
            )
        self._validate_file_storage_archive(archive_path, "object-storage", section)
        return self._extract_prefix(
            archive_path,
            "object-storage",
            Path(self.settings.object_storage_local_path).expanduser(),
        )

    def _restore_s3_object_storage(
        self,
        archive_path: Path,
        prefix: str,
        section: dict,
        validation: dict,
    ) -> dict:
        store = self._s3_store()
        store._ensure_bucket()
        restored_count = 0
        restored_bytes = 0
        expected_objects = self._s3_restore_object_metadata(section)
        client = store.client
        with tarfile.open(archive_path, "r:gz") as archive:
            members = self._s3_archive_members(archive, prefix)

            for member in members:
                key = self._safe_s3_key(member.name.removeprefix(f"{prefix}/"))
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                payload = handle.read()
                metadata = expected_objects.get(key, {})
                content_type = metadata.get("content_type")
                if not isinstance(content_type, str) or not content_type.strip():
                    content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
                client.put_object(
                    Bucket=self.settings.object_storage_bucket,
                    Key=key,
                    Body=payload,
                    ContentType=content_type,
                )
                restored_count += 1
                restored_bytes += len(payload)
        return {
            "file_count": restored_count,
            "total_bytes": restored_bytes,
            "backend": self.settings.object_storage_backend,
            "bucket": self.settings.object_storage_bucket,
            "source": "s3_bucket",
            "manifest_object_count": len(expected_objects),
            "size_verified_count": validation["size_verified_count"],
            "checksum_verified_count": validation["checksum_verified_count"],
        }

    def _validate_object_storage_restore_archive(
        self,
        archive_path: Path,
        section: dict,
        request: BackupRestoreRequest,
    ) -> dict | None:
        if not request.restore_object_storage or not section.get("included"):
            return None
        if self._is_s3_backend() and section.get("archive_prefix") == "object-storage-s3":
            return self._validate_s3_object_storage_archive(
                archive_path,
                "object-storage-s3",
                section,
            )
        return self._validate_file_storage_archive(archive_path, "object-storage", section)

    def _validate_s3_object_storage_archive(
        self,
        archive_path: Path,
        prefix: str,
        section: dict,
    ) -> dict:
        expected_objects = self._s3_restore_object_metadata(section)
        size_verified_count = 0
        checksum_verified_count = 0
        total_bytes = 0
        with tarfile.open(archive_path, "r:gz") as archive:
            members = self._s3_archive_members(archive, prefix)
            archive_keys = {
                self._safe_s3_key(member.name.removeprefix(f"{prefix}/"))
                for member in members
            }
            missing_keys = sorted(set(expected_objects) - archive_keys)
            if missing_keys:
                raise ValueError(f"S3 backup object missing from archive: {missing_keys[0]}")
            unexpected_keys = sorted(archive_keys - set(expected_objects))
            if expected_objects and unexpected_keys:
                raise ValueError(
                    f"S3 backup object is not listed in manifest: {unexpected_keys[0]}"
                )

            for member in members:
                key = self._safe_s3_key(member.name.removeprefix(f"{prefix}/"))
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                payload = handle.read()
                total_bytes += len(payload)
                metadata = expected_objects.get(key, {})
                expected_size = metadata.get("size_bytes")
                if isinstance(expected_size, int):
                    if len(payload) != expected_size:
                        raise ValueError(f"S3 backup object size mismatch for {key}")
                    size_verified_count += 1
                expected_checksum = metadata.get("checksum")
                if isinstance(expected_checksum, str) and expected_checksum.startswith("sha256:"):
                    actual_checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
                    if actual_checksum != expected_checksum:
                        raise ValueError(f"S3 backup object checksum mismatch for {key}")
                    checksum_verified_count += 1
        return {
            "schema_version": "s3_object_storage_restore_validation.v1",
            "validated": True,
            "expected_object_count": len(expected_objects),
            "archive_object_count": len(archive_keys),
            "total_bytes": total_bytes,
            "size_verified_count": size_verified_count,
            "checksum_verified_count": checksum_verified_count,
        }

    def _validate_runtime_state_restore_archive(
        self,
        archive_path: Path,
        section: dict,
        request: BackupRestoreRequest,
    ) -> dict | None:
        if not request.restore_runtime_state or not section.get("included"):
            return None
        return self._validate_file_storage_archive(archive_path, "runtime-state", section)

    def _validate_file_storage_archive(
        self,
        archive_path: Path,
        prefix: str,
        section: dict,
    ) -> dict | None:
        expected_files = self._file_restore_metadata(section)
        if not expected_files:
            return None
        size_verified_count = 0
        checksum_verified_count = 0
        total_bytes = 0
        with tarfile.open(archive_path, "r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.startswith(f"{prefix}/")
            ]
            archive_paths = {
                self._safe_relative_archive_path(member.name.removeprefix(f"{prefix}/"))
                for member in members
            }
            missing_paths = sorted(set(expected_files) - archive_paths)
            if missing_paths:
                raise ValueError(f"backup file missing from archive: {prefix}/{missing_paths[0]}")
            unexpected_paths = sorted(archive_paths - set(expected_files))
            if unexpected_paths:
                raise ValueError(
                    f"backup file is not listed in manifest: {prefix}/{unexpected_paths[0]}"
                )

            for member in members:
                relative_path = self._safe_relative_archive_path(
                    member.name.removeprefix(f"{prefix}/")
                )
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                payload = handle.read()
                total_bytes += len(payload)
                metadata = expected_files.get(relative_path, {})
                expected_size = metadata.get("size_bytes")
                if isinstance(expected_size, int):
                    if len(payload) != expected_size:
                        raise ValueError(f"backup file size mismatch for {prefix}/{relative_path}")
                    size_verified_count += 1
                expected_checksum = metadata.get("checksum")
                if isinstance(expected_checksum, str) and expected_checksum.startswith("sha256:"):
                    actual_checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
                    if actual_checksum != expected_checksum:
                        raise ValueError(
                            f"backup file checksum mismatch for {prefix}/{relative_path}"
                        )
                    checksum_verified_count += 1
        return {
            "schema_version": "file_storage_restore_validation.v1",
            "validated": True,
            "prefix": prefix,
            "expected_file_count": len(expected_files),
            "archive_file_count": len(archive_paths),
            "total_bytes": total_bytes,
            "size_verified_count": size_verified_count,
            "checksum_verified_count": checksum_verified_count,
        }

    def _s3_archive_members(
        self,
        archive: tarfile.TarFile,
        prefix: str,
    ) -> list[tarfile.TarInfo]:
        return [
            member
            for member in archive.getmembers()
            if member.isfile() and member.name.startswith(f"{prefix}/")
        ]

    def _s3_restore_object_metadata(self, section: dict) -> dict[str, dict]:
        objects = section.get("objects")
        if not isinstance(objects, list):
            return {}
        metadata = {}
        for item in objects:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if isinstance(key, str):
                metadata[self._safe_s3_key(key)] = item
        return metadata

    def _file_manifest_entries(self, root: Path, files: list[Path]) -> list[dict]:
        entries = []
        for path in files:
            relative_path = self._safe_relative_archive_path(
                path.relative_to(root).as_posix()
            )
            payload = path.read_bytes()
            entries.append(
                {
                    "path": relative_path,
                    "size_bytes": len(payload),
                    "checksum": "sha256:" + hashlib.sha256(payload).hexdigest(),
                }
            )
        return entries

    def _file_restore_metadata(self, section: dict) -> dict[str, dict]:
        files = section.get("files")
        if not isinstance(files, list):
            return {}
        metadata = {}
        for item in files:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str):
                metadata[self._safe_relative_archive_path(path)] = item
        return metadata

    def _safe_relative_archive_path(self, value: str) -> str:
        path = Path(value)
        if (
            not value
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"unsafe backup archive path: {value}")
        return path.as_posix()

    def _resolve_backup_path(self, backup_path: str) -> Path:
        root = self.backup_root.resolve()
        requested = Path(backup_path).expanduser()
        archive_path = requested if requested.is_absolute() else self.backup_root / requested
        resolved = archive_path.resolve()
        if not self._is_relative_to(resolved, root):
            raise ValueError("backup path must resolve inside DIALECTICORE_BACKUP_PATH")
        if not resolved.exists():
            raise ValueError("backup archive does not exist")
        return resolved

    def _is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _is_s3_backend(self) -> bool:
        return self.settings.object_storage_backend.strip().lower() in {
            "s3",
            "s3-compatible",
            "minio",
        }

    def _s3_store(self) -> S3ObjectStore:
        return S3ObjectStore(self.settings, client=self._s3_client)

    def _list_s3_objects(self, client: object) -> list[dict]:
        bucket = self.settings.object_storage_bucket
        if hasattr(client, "get_paginator"):
            objects = []
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                objects.extend(page.get("Contents", []))
            return sorted(objects, key=lambda item: str(item.get("Key", "")))

        objects = []
        kwargs = {"Bucket": bucket}
        while True:
            response = client.list_objects_v2(**kwargs)
            objects.extend(response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
            kwargs["ContinuationToken"] = token
        return sorted(objects, key=lambda item: str(item.get("Key", "")))

    def _safe_s3_key(self, key: str) -> str:
        normalized = key.strip().lstrip("/")
        parts = normalized.split("/")
        if (
            not normalized
            or any(part in {"", ".", ".."} for part in parts)
            or normalized != "/".join(parts)
        ):
            raise ValueError(f"unsafe S3 object key in backup: {key}")
        return normalized

    def _safe_endpoint_metadata(self, endpoint: str) -> str:
        parsed = urlsplit(endpoint.strip())
        if not parsed.netloc:
            return endpoint.strip()
        host = parsed.hostname
        if not host:
            return "[redacted]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(
            SplitResult(
                scheme=parsed.scheme,
                netloc=netloc,
                path=parsed.path,
                query="",
                fragment="",
            )
        )

    def _enrich_manifest(self, manifest: dict, archive_path: Path) -> dict:
        return manifest | {"archive": self._archive_info(archive_path)}

    def _backup_restore_validation(
        self,
        manifest: dict,
        archive_path: Path,
        audit_events: list[AuditEvent],
    ) -> dict:
        backup_id = manifest.get("backup_id")
        for event in audit_events:
            if event.event_type != "backup.restore_validated":
                continue
            details = event.details or {}
            if backup_id and details.get("backup_id") != backup_id:
                continue
            audited_path = str(details.get("archive_path") or "")
            if audited_path and Path(audited_path).name != archive_path.name:
                continue
            expected_checksum = (manifest.get("archive") or {}).get("checksum")
            audited_checksum = details.get("archive_checksum")
            if not expected_checksum:
                return {
                    "validated": False,
                    "status": "checksum_not_evaluated",
                    "reason": (
                        "backup archive checksum was not evaluated because the archive "
                        "exceeds the listing checksum limit"
                    ),
                    "backup_id": backup_id,
                    "validated_archive_checksum": audited_checksum,
                }
            if expected_checksum and audited_checksum != expected_checksum:
                return {
                    "validated": False,
                    "status": "checksum_mismatch",
                    "reason": (
                        "latest backup archive checksum does not match the "
                        "recorded dry-run restore validation"
                    ),
                    "backup_id": backup_id,
                    "archive_checksum": expected_checksum,
                    "validated_archive_checksum": audited_checksum,
                }
            restore_plan = details.get("restore_plan") or {}
            summary = restore_plan.get("summary") or {}
            validated_at = event.created_at
            if validated_at.tzinfo is None:
                validated_at = validated_at.replace(tzinfo=UTC)
            return {
                "validated": True,
                "status": "validated",
                "validated_at": validated_at.isoformat(),
                "validation_age_seconds": max(
                    0.0,
                    (datetime.now(UTC) - validated_at).total_seconds(),
                ),
                "actor": event.actor,
                "archive_checksum": details.get("archive_checksum"),
                "restore_plan_schema_version": restore_plan.get("schema_version"),
                "target_scope_count": summary.get("target_scope_count"),
                "target_record_count": summary.get("target_record_count"),
                "target_file_count": summary.get("target_file_count"),
            }
        return {
            "validated": False,
            "status": "missing",
            "reason": "no backup.restore_validated audit event found for archive",
        }

    def _archive_info(
        self,
        archive_path: Path,
        checksum_max_bytes: int | None = None,
    ) -> dict:
        size_bytes = archive_path.stat().st_size
        info = {
            "path": str(archive_path),
            "filename": archive_path.name,
            "size_bytes": size_bytes,
        }
        if checksum_max_bytes is not None and size_bytes > checksum_max_bytes:
            return info | {
                "checksum": None,
                "checksum_status": "skipped",
                "checksum_skipped_reason": "archive_exceeds_listing_checksum_limit",
                "checksum_max_bytes": checksum_max_bytes,
            }
        return info | {
            "checksum": self._sha256(archive_path),
            "checksum_status": "computed",
        }

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
