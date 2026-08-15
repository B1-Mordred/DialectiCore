#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_EVIDENCE_OUTPUT = "output/smoke/backup-smoke-evidence.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and dry-run validate a DialectiCore backup archive."
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--label", default="backup-smoke")
    parser.add_argument("--user-id", default="backup-smoke")
    parser.add_argument("--no-object-storage", action="store_true")
    parser.add_argument("--no-runtime-state", action="store_true")
    parser.add_argument(
        "--evidence-output",
        default=DEFAULT_EVIDENCE_OUTPUT,
        help="Where to write stable JSON evidence.",
    )
    parser.add_argument("--no-evidence-file", action="store_true")
    args = parser.parse_args()

    api_base = args.api_base.rstrip("/")
    result: dict[str, Any] = {
        "schema_version": "backup_smoke_evidence.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "api_base": api_base,
        "label": args.label,
        "include_object_storage": not args.no_object_storage,
        "include_runtime_state": not args.no_runtime_state,
    }
    try:
        with httpx.Client(timeout=300) as client:
            created = create_backup(
                client,
                api_base,
                label=args.label,
                user_id=args.user_id,
                include_object_storage=not args.no_object_storage,
                include_runtime_state=not args.no_runtime_state,
            )
            result["created_backup"] = backup_summary(created)
            archive_filename = str(created.get("archive", {}).get("filename") or "")
            if not archive_filename:
                raise ValueError("created backup did not include archive filename")
            validation = validate_backup(
                client,
                api_base,
                archive_filename,
                user_id=args.user_id,
                restore_object_storage=not args.no_object_storage,
                restore_runtime_state=not args.no_runtime_state,
            )
            result["restore_validation"] = validation_summary(validation)
            backups = get_json(client, f"{api_base}/api/v1/system/backups")
            result["listed_backup"] = listed_backup_summary(backups, created.get("backup_id"))
            readiness = get_json(client, f"{api_base}/api/v1/system/live-provider-readiness")
            result["backup_readiness"] = backup_readiness_summary(readiness)
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        emit_result(result, args)
        return 1

    failed_sections = []
    if result["created_backup"].get("status") != "pass":
        failed_sections.append("created_backup")
    if result["restore_validation"].get("status") != "validated":
        failed_sections.append("restore_validation")
    if result["backup_readiness"].get("status") != "pass":
        failed_sections.append("backup_readiness")
    result["status"] = "fail" if failed_sections else "pass"
    result["failed_sections"] = failed_sections
    emit_result(result, args)
    return 1 if failed_sections else 0


def create_backup(
    client: httpx.Client,
    api_base: str,
    *,
    label: str,
    user_id: str,
    include_object_storage: bool,
    include_runtime_state: bool,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/system/backups",
        json={
            "label": label,
            "user_id": user_id,
            "include_object_storage": include_object_storage,
            "include_runtime_state": include_runtime_state,
        },
    )
    response.raise_for_status()
    return response.json()


def validate_backup(
    client: httpx.Client,
    api_base: str,
    backup_path: str,
    *,
    user_id: str,
    restore_object_storage: bool,
    restore_runtime_state: bool,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/system/backups/restore",
        json={
            "backup_path": backup_path,
            "apply": False,
            "restore_database": False,
            "restore_object_storage": restore_object_storage,
            "restore_runtime_state": restore_runtime_state,
            "user_id": user_id,
        },
    )
    response.raise_for_status()
    return response.json()


def get_json(client: httpx.Client, url: str) -> Any:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def backup_summary(backup: dict[str, Any]) -> dict[str, Any]:
    archive = backup.get("archive") if isinstance(backup.get("archive"), dict) else {}
    database = backup.get("database") if isinstance(backup.get("database"), dict) else {}
    object_storage = (
        backup.get("object_storage") if isinstance(backup.get("object_storage"), dict) else {}
    )
    runtime_state = (
        backup.get("runtime_state") if isinstance(backup.get("runtime_state"), dict) else {}
    )
    return {
        "status": "pass" if archive.get("filename") and archive.get("checksum") else "fail",
        "backup_id": backup.get("backup_id"),
        "created_at": backup.get("created_at"),
        "archive_filename": archive.get("filename"),
        "archive_checksum": archive.get("checksum"),
        "archive_size_bytes": archive.get("size_bytes"),
        "database_total_records": database.get("total_records"),
        "object_storage_file_count": object_storage.get("file_count"),
        "runtime_state_file_count": runtime_state.get("file_count"),
    }


def validation_summary(validation: dict[str, Any]) -> dict[str, Any]:
    restore_plan = (
        validation.get("restore_plan") if isinstance(validation.get("restore_plan"), dict) else {}
    )
    summary = restore_plan.get("summary") if isinstance(restore_plan.get("summary"), dict) else {}
    object_storage = (
        restore_plan.get("object_storage")
        if isinstance(restore_plan.get("object_storage"), dict)
        else {}
    )
    runtime_state = (
        restore_plan.get("runtime_state")
        if isinstance(restore_plan.get("runtime_state"), dict)
        else {}
    )
    return {
        "status": validation.get("status"),
        "apply": validation.get("apply"),
        "backup_id": restore_plan.get("backup_id"),
        "restore_plan_schema_version": restore_plan.get("schema_version"),
        "target_scope_count": summary.get("target_scope_count"),
        "target_record_count": summary.get("target_record_count"),
        "target_file_count": summary.get("target_file_count"),
        "object_storage": archive_validation_summary(object_storage),
        "runtime_state": archive_validation_summary(runtime_state),
    }


def archive_validation_summary(section: dict[str, Any]) -> dict[str, Any]:
    validation = (
        section.get("archive_validation")
        if isinstance(section.get("archive_validation"), dict)
        else {}
    )
    status = validation.get("status")
    if status is None and validation.get("validated") is True:
        status = "validated"
    return {
        "will_restore": section.get("will_restore"),
        "validated": validation.get("validated"),
        "status": status,
        "schema_version": validation.get("schema_version"),
        "checksum_verified_count": validation.get("checksum_verified_count"),
    }


def listed_backup_summary(backups_response: Any, backup_id: object) -> dict[str, Any]:
    backups = backups_response.get("backups", []) if isinstance(backups_response, dict) else []
    match = next(
        (
            backup
            for backup in backups
            if isinstance(backup, dict) and backup.get("backup_id") == backup_id
        ),
        None,
    )
    validation = (
        match.get("restore_validation")
        if isinstance(match, dict) and isinstance(match.get("restore_validation"), dict)
        else {}
    )
    return {
        "found": match is not None,
        "backup_id": backup_id,
        "restore_validation_status": validation.get("status"),
        "restore_validation_validated": validation.get("validated"),
    }


def backup_readiness_summary(readiness: dict[str, Any]) -> dict[str, Any]:
    backup = next(
        (
            check
            for check in readiness.get("checks", [])
            if isinstance(check, dict) and check.get("category") == "backup_storage"
        ),
        {},
    )
    details = backup.get("details") if isinstance(backup.get("details"), dict) else {}
    latest = (
        details.get("latest_restore_validation")
        if isinstance(details.get("latest_restore_validation"), dict)
        else {}
    )
    return {
        "status": backup.get("status"),
        "blockers": backup.get("blockers", []),
        "warnings": backup.get("warnings", []),
        "archive_count": details.get("archive_count"),
        "restore_validated_archive_count": details.get("restore_validated_archive_count"),
        "failed_readiness_checks": details.get("failed_readiness_checks", []),
        "latest_restore_validation_status": latest.get("status"),
    }


def emit_result(result: dict[str, Any], args: argparse.Namespace) -> None:
    output = {"result": result}
    if not args.no_evidence_file:
        path = Path(args.evidence_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(payload)
        output["evidence_file"] = {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
