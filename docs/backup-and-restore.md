# Backup and Restore

Increment 6 adds a first runnable backup and restore control plane for the
self-hosted stack.

## Archive Contents

`POST /api/v1/system/backups` writes a `dialecticore-backup-*.tar.gz` archive
under `DIALECTICORE_BACKUP_PATH`. Each archive contains:

- `manifest.json`: backup ID, creator, database record counts, object-storage
  file counts, runtime-state file counts, archive filename, size, and checksum.
- `database.json`: exported rows for episode aggregates, audit events, model
  endpoints, participant profiles, Voicebox endpoints/profiles, ComfyUI
  endpoints/workflows, visual profiles, and publisher targets.
- `object-storage/`: local object-storage files from
  `DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH` when the local/filesystem backend is
  active and object storage is requested. New archives record each relative file
  path, byte size, and SHA-256 checksum.
- `object-storage-s3/`: authoritative remote S3/MinIO bucket objects fetched
  through the configured S3 API when the S3-compatible backend is active and
  object storage is requested.
- `runtime-state/`: worker heartbeat and other runtime-state files from
  `DIALECTICORE_RUNTIME_STATE_PATH` when requested and present. New archives
  record each relative file path, byte size, and SHA-256 checksum.

For S3/MinIO deployments, the manifest records `source=s3_bucket`, a sanitized
endpoint location, region, object keys, object checksums, and the
`object-storage-s3` archive prefix. Endpoint URL userinfo, query strings, and
fragments are removed before the manifest is written or returned. Restore
uploads archived objects back to the currently configured bucket with
`put_object` after verifying each manifest-listed archive member's size and
checksum. The recorded content type is preserved during upload. The local probe
cache path remains metadata only for S3 backups and is not treated as
authoritative bucket state.

## API Workflow

Create a backup:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/system/backups \
  -H 'content-type: application/json' \
  --data '{"label":"nightly","user_id":"operator"}'
```

List backups:

```bash
curl http://127.0.0.1:8000/api/v1/system/backups
```

Each listed archive includes a `restore_validation` block derived from recent
`backup.restore_validated` audit events so operators can see whether that exact
archive checksum has been dry-run restore tested, along with the validation age,
actor, restore-plan schema, target counts, and checksum. If an archive file is
replaced or modified after validation, the status becomes `checksum_mismatch`
until the current archive is validated again.

Validate a restore without applying it:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/system/backups/restore \
  -H 'content-type: application/json' \
  --data '{"backup_path":"dialecticore-backup-YYYYMMDDTHHMMSSZ-nightly.tar.gz"}'
```

Dry-run validation returns a versioned `backup_restore_plan.v1` block showing
which scopes would be restored, whether object-storage/runtime-state payloads
are present in the archive, target database record counts, target file counts,
and the selected `replace_existing` policy. Dry-run validations emit a
`backup.restore_validated` global audit event with the archive checksum and
restore plan, without applying database, object-storage, or runtime-state
changes.
When S3/MinIO object storage is selected for restore, the dry-run also validates
the manifest-listed archived objects and includes a versioned
`s3_object_storage_restore_validation.v1` evidence block in the restore plan.
For local filesystem object-storage and runtime-state restores, dry-run
validation uses the same manifest-backed approach and includes
`file_storage_restore_validation.v1` evidence when the archive was created with
file metadata.

Apply a restore:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/system/backups/restore \
  -H 'content-type: application/json' \
  --data '{
    "backup_path":"dialecticore-backup-YYYYMMDDTHHMMSSZ-nightly.tar.gz",
    "apply":true,
    "restore_database":true,
    "restore_object_storage":true,
    "restore_runtime_state":false,
    "replace_existing":true,
    "user_id":"operator"
  }'
```

Restore paths are constrained to `DIALECTICORE_BACKUP_PATH`; archive extraction
guards against path traversal and rejects unsafe S3 object keys. Runtime-state
restore defaults to `false` because stale heartbeat files should usually not be
restored during disaster recovery.

Archives missing required `manifest.json` or `database.json` members are rejected
with explicit validation errors. Those root metadata members must each appear
exactly once as regular files; duplicate entries or link entries are rejected so
restore planning cannot depend on ambiguous tar metadata. Incomplete or invalid
archives are skipped by backup listing rather than blocking inspection of valid
archives in the same directory.
For S3/MinIO restores, manifest-listed objects must also be present in the
archive, no additional archive object keys are accepted when a manifest object
list is available, and each object must match its recorded size and SHA-256
checksum before any upload is trusted.
For local filesystem object-storage and runtime-state restores, manifest-listed
files must also be present, no additional files under the restored prefix are
accepted when file metadata is available, and size/checksum mismatches are
rejected before extraction.

## Docker Compose

Docker Compose mounts the `backups` volume at `/data/backups` in
`production-api` and sets `DIALECTICORE_BACKUP_PATH=/data/backups`. The
`object-storage` and `runtime-state` volumes are mounted separately. For a full
host-level recovery point, back up:

- PostgreSQL or the DialectiCore backup archive database payload.
- `object-storage` for local deployments or the S3/MinIO bucket captured in the
  API archive for S3-compatible deployments.
- `runtime-state` when operational heartbeats are useful for incident analysis.
- `backups` so the API-generated archives survive container replacement.

Backup creation, dry-run restore validation, and applied restores emit global
audit events: `backup.created`, `backup.restore_validated`, and
`backup.restored`.

## Health And Metrics

`GET /api/v1/system/health` includes a `backup_storage` component. It reports
the configured backup path, the checked target or parent path, whether that
checked path exists, is a directory, and is writable by the API process,
archive count, and the latest archive's filename, size, age, backup ID, and
`manifest.json` readability. It also links the latest archive to the newest
matching `backup.restore_validated` audit event for the current archive checksum,
including validation time, actor, restore-plan schema, target scope count,
target record count, target file count, archive checksum, and safe per-scope
object-storage/runtime-state content-validation summaries when the dry-run
restore plan contains them. The component also counts readable archives, unreadable
archives, restore-validated archives, and readable archives without restore
validation evidence. Checksum mismatches are treated as missing current
validation evidence. A missing or unwritable backup path, an empty archive
directory, an unreadable latest manifest, or a latest archive without dry-run
restore validation evidence degrades system health so operators can catch backup
drift before a restore is needed. The Web UI Backups panel shows the same component
status beside backup creation/listing controls, including archive count,
validation coverage, latest manifest readability, restore-validation status,
latest archive age, object-storage/runtime-state content-validation summaries,
and the operator-facing reason from the health component.

`GET /api/v1/system/metrics` exposes the same readiness evidence for headless
monitoring:

- `dialecticore_backup_archive_count`
- `dialecticore_backup_archive_validation_count`
- `dialecticore_backup_latest_archive_info`
- `dialecticore_backup_latest_age_seconds`
- `dialecticore_backup_latest_size_bytes`
- `dialecticore_backup_latest_restore_validated`
- `dialecticore_backup_latest_restore_validation_age_seconds`
- `dialecticore_backup_latest_content_validation`
