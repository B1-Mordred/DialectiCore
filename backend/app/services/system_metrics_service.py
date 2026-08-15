from __future__ import annotations

from app.domain.schemas import WorkerStatusSummary


class SystemMetricsService:
    def render(self, health: dict, workers: WorkerStatusSummary) -> str:
        status = self._label(str(health.get("status", "unknown")))
        lines = [
            "# HELP dialecticore_system_health_status Overall API health status.",
            "# TYPE dialecticore_system_health_status gauge",
            f'dialecticore_system_health_status{{status="{status}"}} 1',
            "# HELP dialecticore_component_health_status Component health status.",
            "# TYPE dialecticore_component_health_status gauge",
        ]
        for component in health.get("components", []):
            component_name = self._label(str(component.get("name", "unknown")))
            component_status = self._label(str(component.get("status", "unknown")))
            lines.append(
                "dialecticore_component_health_status"
                f'{{component="{component_name}",status="{component_status}"}} 1'
            )
        lines.extend(
            [
                "# HELP dialecticore_component_readiness_check "
                "Normalized component readiness check results.",
                "# TYPE dialecticore_component_readiness_check gauge",
            ]
        )
        for component in health.get("components", []):
            component_name = self._label(str(component.get("name", "unknown")))
            details = component.get("details") or {}
            checks = details.get("readiness_checks") if isinstance(details, dict) else None
            if not isinstance(checks, dict):
                continue
            for check_name, value in sorted(checks.items()):
                if not isinstance(value, bool):
                    continue
                check_status = "pass" if value else "fail"
                lines.append(
                    "dialecticore_component_readiness_check"
                    f'{{component="{component_name}",'
                    f'check="{self._label(str(check_name))}",'
                    f'status="{check_status}"}} 1'
                )
        database_migrations = self._component(health, "database_migrations")
        if database_migrations is not None:
            migration_details = database_migrations.get("details") or {}
            current_revisions = migration_details.get("current_revisions") or []
            head_revisions = migration_details.get("head_revisions") or []
            current = ",".join(str(item) for item in current_revisions)
            head = ",".join(str(item) for item in head_revisions)
            schema_current = (migration_details.get("readiness_checks") or {}).get(
                "database_schema_at_head"
            ) is True
            lines.extend(
                [
                    "# HELP dialecticore_database_migration_status "
                    "Alembic database migration revision status.",
                    "# TYPE dialecticore_database_migration_status gauge",
                    "dialecticore_database_migration_status"
                    f'{{status="{self._label(str(database_migrations.get("status", "unknown")))}",'
                    f'enforced="{str(migration_details.get("enforced") is True).lower()}",'
                    f'current="{self._label(current)}",'
                    f'head="{self._label(head)}"}} {1 if schema_current else 0}',
                ]
            )
        redis_runtime = self._component(health, "redis")
        if redis_runtime is not None:
            redis_details = redis_runtime.get("details") or {}
            redis_probe = redis_details.get("tcp_probe") or {}
            lines.extend(
                [
                    "# HELP dialecticore_redis_runtime_enabled "
                    "Redis-backed event fan-out and worker-signal mode flags.",
                    "# TYPE dialecticore_redis_runtime_enabled gauge",
                    'dialecticore_redis_runtime_enabled{mode="event_fanout"} '
                    f"{1 if redis_details.get('event_fanout_enabled') is True else 0}",
                    'dialecticore_redis_runtime_enabled{mode="worker_signal"} '
                    f"{1 if redis_details.get('worker_signal_enabled') is True else 0}",
                    "# HELP dialecticore_redis_runtime_reachable "
                    "Redis TCP reachability for enabled runtime channels.",
                    "# TYPE dialecticore_redis_runtime_reachable gauge",
                    "dialecticore_redis_runtime_reachable"
                    f'{{host="{self._label(str(redis_probe.get("host") or ""))}",'
                    f'port="{self._label(str(redis_probe.get("port") or ""))}",'
                    f'event_channel="{self._label(str(redis_details.get("event_channel") or ""))}",'
                    f'worker_signal_stream="'
                    f'{self._label(str(redis_details.get("worker_signal_stream") or ""))}"}} '
                    f"{1 if redis_probe.get('reachable') is True else 0}",
                    "# HELP dialecticore_redis_worker_signal_maxlen "
                    "Configured Redis worker-signal stream retention cap.",
                    "# TYPE dialecticore_redis_worker_signal_maxlen gauge",
                    "dialecticore_redis_worker_signal_maxlen "
                    f"{int(redis_details.get('worker_signal_maxlen') or 0)}",
                ]
            )
        object_storage = self._component(health, "object_storage")
        if object_storage is not None:
            object_details = object_storage.get("details") or {}
            tcp_probe = object_details.get("tcp_probe") or {}
            checked_path = object_details.get("checked_path")
            if checked_path:
                writable_target_or_parent = (
                    object_details.get("writable_target_or_parent")
                    if "writable_target_or_parent" in object_details
                    else object_details.get("writable_parent")
                )
                local_labels = (
                    f'backend="{self._label(str(object_details.get("backend") or ""))}",'
                    f'bucket="{self._label(str(object_details.get("bucket") or ""))}",'
                    f'checked_path="{self._label(str(checked_path))}"'
                )
                local_ready = (
                    object_details.get("checked_path_exists") is True
                    and object_details.get("checked_path_is_dir") is True
                    and writable_target_or_parent is True
                )
                lines.extend(
                    [
                        "# HELP dialecticore_object_storage_local_path_ready "
                        "Local object-storage checked-path readiness.",
                        "# TYPE dialecticore_object_storage_local_path_ready gauge",
                        "dialecticore_object_storage_local_path_ready"
                        f"{{{local_labels}}} {1 if local_ready else 0}",
                        "# HELP dialecticore_object_storage_local_path_state "
                        "Local object-storage checked-path boolean state.",
                        "# TYPE dialecticore_object_storage_local_path_state gauge",
                    ]
                )
                for state_name, value in {
                    "path_exists": object_details.get("path_exists") is True,
                    "parent_exists": object_details.get("parent_exists") is True,
                    "checked_path_exists": object_details.get("checked_path_exists") is True,
                    "checked_path_is_dir": object_details.get("checked_path_is_dir") is True,
                    "writable_target_or_parent": writable_target_or_parent is True,
                }.items():
                    lines.append(
                        "dialecticore_object_storage_local_path_state"
                        f'{{{local_labels},state="{self._label(state_name)}"}} '
                        f"{1 if value else 0}"
                    )
            if tcp_probe:
                bucket_probe = object_details.get("bucket_probe") or {}
                lines.extend(
                    [
                        "# HELP dialecticore_object_storage_remote_reachable "
                        "Remote object-storage endpoint TCP reachability.",
                        "# TYPE dialecticore_object_storage_remote_reachable gauge",
                        "dialecticore_object_storage_remote_reachable"
                        f'{{backend="{self._label(str(object_details.get("backend") or ""))}",'
                        f'bucket="{self._label(str(object_details.get("bucket") or ""))}",'
                        f'host="{self._label(str(tcp_probe.get("host") or ""))}",'
                        f'port="{self._label(str(tcp_probe.get("port") or ""))}"}} '
                        f"{1 if tcp_probe.get('reachable') is True else 0}",
                        "# HELP dialecticore_object_storage_bucket_available "
                        "Configured remote object-storage bucket availability.",
                        "# TYPE dialecticore_object_storage_bucket_available gauge",
                        "dialecticore_object_storage_bucket_available"
                        f'{{backend="{self._label(str(object_details.get("backend") or ""))}",'
                        f'bucket="{self._label(str(object_details.get("bucket") or ""))}",'
                        f'probe="{self._label(str(bucket_probe.get("probe") or ""))}"}} '
                        f"{1 if object_details.get('bucket_available') is True else 0}",
                    ]
                )
        deployment = self._component(health, "deployment_readiness")
        if deployment is not None:
            deployment_details = deployment.get("details") or {}
            deployment_checks = deployment_details.get("checks") or {}
            deployment_status = self._label(str(deployment.get("status") or "unknown"))
            lines.extend(
                [
                    "# HELP dialecticore_deployment_readiness_status "
                    "Production deployment readiness status.",
                    "# TYPE dialecticore_deployment_readiness_status gauge",
                    "dialecticore_deployment_readiness_status"
                    f'{{env="{self._label(str(deployment_details.get("env") or ""))}",'
                    f'status="{deployment_status}"}} 1',
                    "# HELP dialecticore_deployment_readiness_issues "
                    "Count of deployment readiness issues.",
                    "# TYPE dialecticore_deployment_readiness_issues gauge",
                    "dialecticore_deployment_readiness_issues "
                    f"{int(deployment_details.get('issue_count') or 0)}",
                    "# HELP dialecticore_deployment_readiness_check "
                    "Individual deployment readiness check results.",
                    "# TYPE dialecticore_deployment_readiness_check gauge",
                ]
            )
            for name, value in sorted(deployment_checks.items()):
                if isinstance(value, bool):
                    check_status = "pass" if value else "fail"
                    lines.append(
                        "dialecticore_deployment_readiness_check"
                        f'{{check="{self._label(str(name))}",status="{check_status}"}} 1'
                    )
        runtime_paths = self._component(health, "runtime_paths")
        if runtime_paths is not None:
            runtime_path_details = runtime_paths.get("details") or {}
            runtime_path_items = runtime_path_details.get("paths") or {}
            lines.extend(
                [
                    "# HELP dialecticore_runtime_path_ready "
                    "Required runtime path target or parent writability.",
                    "# TYPE dialecticore_runtime_path_ready gauge",
                ]
            )
            for name, details in sorted(runtime_path_items.items()):
                if not isinstance(details, dict):
                    continue
                required = "true" if details.get("required") is True else "false"
                ready = (
                    details.get("path_configured") is True
                    and details.get("parent_exists") is True
                    and details.get("writable_target_or_parent") is True
                )
                lines.append(
                    "dialecticore_runtime_path_ready"
                    f'{{name="{self._label(str(name))}",required="{required}"}} '
                    f"{1 if ready else 0}"
                )
            lines.extend(
                [
                    "# HELP dialecticore_runtime_path_state "
                    "Required runtime path boolean state by path and state dimension.",
                    "# TYPE dialecticore_runtime_path_state gauge",
                ]
            )
            for name, details in sorted(runtime_path_items.items()):
                if not isinstance(details, dict):
                    continue
                required = "true" if details.get("required") is True else "false"
                for state_name, value in {
                    "path_configured": details.get("path_configured") is True,
                    "path_exists": details.get("path_exists") is True,
                    "parent_exists": details.get("parent_exists") is True,
                    "checked_path_exists": details.get("checked_path_exists") is True,
                    "checked_path_is_dir": details.get("checked_path_is_dir") is True,
                    "writable_target_or_parent": details.get("writable_target_or_parent") is True,
                    "free_bytes_sufficient": details.get("free_bytes_sufficient") is True,
                }.items():
                    lines.append(
                        "dialecticore_runtime_path_state"
                        f'{{name="{self._label(str(name))}",required="{required}",'
                        f'state="{self._label(state_name)}"}} {1 if value else 0}'
                    )
            lines.extend(
                [
                    "# HELP dialecticore_runtime_path_free_bytes_sufficient "
                    "Whether required runtime paths meet the configured free-space floor.",
                    "# TYPE dialecticore_runtime_path_free_bytes_sufficient gauge",
                ]
            )
            for name, details in sorted(runtime_path_items.items()):
                if not isinstance(details, dict):
                    continue
                required = "true" if details.get("required") is True else "false"
                sufficient = details.get("free_bytes_sufficient") is True
                lines.append(
                    "dialecticore_runtime_path_free_bytes_sufficient"
                    f'{{name="{self._label(str(name))}",required="{required}"}} '
                    f"{1 if sufficient else 0}"
                )
            lines.extend(
                [
                    "# HELP dialecticore_runtime_path_free_bytes "
                    "Free bytes on the checked runtime path target or parent directory.",
                    "# TYPE dialecticore_runtime_path_free_bytes gauge",
                ]
            )
            for name, details in sorted(runtime_path_items.items()):
                if not isinstance(details, dict):
                    continue
                free_bytes = details.get("free_bytes")
                if free_bytes is None:
                    continue
                required = "true" if details.get("required") is True else "false"
                lines.append(
                    "dialecticore_runtime_path_free_bytes"
                    f'{{name="{self._label(str(name))}",required="{required}"}} '
                    f"{int(free_bytes)}"
                )
        credentials = self._component(health, "credential_references")
        if credentials is not None:
            credential_details = credentials.get("details") or {}
            lines.extend(
                [
                    "# HELP dialecticore_credential_reference_count "
                    "Active credential-reference readiness counters.",
                    "# TYPE dialecticore_credential_reference_count gauge",
                    'dialecticore_credential_reference_count{dimension="status",value="checked"} '
                    f"{int(credential_details.get('checked_count') or 0)}",
                    'dialecticore_credential_reference_count{dimension="status",value="resolved"} '
                    f"{int(credential_details.get('resolved_count') or 0)}",
                    "dialecticore_credential_reference_count"
                    '{dimension="status",value="unavailable"} '
                    f"{int(credential_details.get('unavailable_count') or 0)}",
                ]
            )
            for dimension, counts in {
                "owner_type": credential_details.get("by_owner_type") or {},
                "scheme": credential_details.get("by_scheme") or {},
            }.items():
                for value, count in sorted(counts.items()):
                    lines.append(
                        "dialecticore_credential_reference_count"
                        f'{{dimension="{self._label(str(dimension))}",'
                        f'value="{self._label(str(value))}"}} {int(count)}'
                    )
        credential_provisioning = self._component(health, "credential_provisioning")
        if credential_provisioning is not None:
            provisioning_details = credential_provisioning.get("details") or {}
            lines.extend(
                [
                    "# HELP dialecticore_credential_provisioning_count "
                    "Credential provisioning readiness counters without secret values.",
                    "# TYPE dialecticore_credential_provisioning_count gauge",
                    'dialecticore_credential_provisioning_count{scope="active",kind="references"} '
                    f"{int(provisioning_details.get('active_reference_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="active",kind="unavailable"} '
                    f"{int(provisioning_details.get('active_unavailable_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="references"} '
                    f"{int(provisioning_details.get('all_reference_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="unavailable"} '
                    f"{int(provisioning_details.get('all_unavailable_count') or 0)}",
                    "dialecticore_credential_provisioning_count"
                    '{scope="inactive",kind="unavailable"} '
                    f"{int(provisioning_details.get('inactive_unavailable_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="env_vars"} '
                    f"{int(provisioning_details.get('env_var_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="docker_secrets"} '
                    f"{int(provisioning_details.get('docker_secret_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="files"} '
                    f"{int(provisioning_details.get('file_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="unsupported"} '
                    f"{int(provisioning_details.get('unsupported_count') or 0)}",
                    'dialecticore_credential_provisioning_count{scope="all",kind="invalid"} '
                    f"{int(provisioning_details.get('invalid_count') or 0)}",
                ]
            )
        backup = self._component(health, "backup_storage")
        if backup is not None:
            backup_details = backup.get("details") or {}
            latest_archive = backup_details.get("latest_archive") or {}
            latest_validation = backup_details.get("latest_restore_validation") or {}
            latest_filename = str(latest_archive.get("filename") or "")
            manifest_readable = latest_archive.get("manifest_readable") is True
            restore_validated = latest_validation.get("validated") is True
            restore_plan_schema = self._label(
                str(latest_validation.get("restore_plan_schema_version") or "")
            )
            object_storage_validation = (
                latest_validation.get("object_storage_archive_validation") or {}
            )
            runtime_state_validation = (
                latest_validation.get("runtime_state_archive_validation") or {}
            )
            lines.extend(
                [
                    "# HELP dialecticore_backup_archive_count Backup archive count.",
                    "# TYPE dialecticore_backup_archive_count gauge",
                    "dialecticore_backup_archive_count "
                    f"{int(backup_details.get('archive_count') or 0)}",
                    "# HELP dialecticore_backup_archive_validation_count "
                    "Backup archive restore-validation coverage counters.",
                    "# TYPE dialecticore_backup_archive_validation_count gauge",
                    'dialecticore_backup_archive_validation_count{status="readable"} '
                    f"{int(backup_details.get('readable_archive_count') or 0)}",
                    'dialecticore_backup_archive_validation_count{status="validated"} '
                    f"{int(backup_details.get('restore_validated_archive_count') or 0)}",
                    'dialecticore_backup_archive_validation_count{status="unvalidated"} '
                    f"{int(backup_details.get('restore_unvalidated_archive_count') or 0)}",
                    'dialecticore_backup_archive_validation_count{status="unreadable"} '
                    f"{int(backup_details.get('unreadable_archive_count') or 0)}",
                    "# HELP dialecticore_backup_latest_archive_info "
                    "Latest backup archive manifest status.",
                    "# TYPE dialecticore_backup_latest_archive_info gauge",
                    "dialecticore_backup_latest_archive_info"
                    f'{{filename="{self._label(latest_filename)}",'
                    f'manifest_readable="{str(manifest_readable).lower()}"}} '
                    f"{1 if latest_filename else 0}",
                    "# HELP dialecticore_backup_latest_age_seconds "
                    "Age of the latest backup archive.",
                    "# TYPE dialecticore_backup_latest_age_seconds gauge",
                    "dialecticore_backup_latest_age_seconds "
                    f"{float(latest_archive.get('age_seconds') or 0):.3f}",
                    "# HELP dialecticore_backup_latest_size_bytes "
                    "Size of the latest backup archive.",
                    "# TYPE dialecticore_backup_latest_size_bytes gauge",
                    "dialecticore_backup_latest_size_bytes "
                    f"{int(latest_archive.get('size_bytes') or 0)}",
                    "# HELP dialecticore_backup_latest_restore_validated "
                    "Whether the latest backup archive has dry-run restore validation evidence.",
                    "# TYPE dialecticore_backup_latest_restore_validated gauge",
                    "dialecticore_backup_latest_restore_validated"
                    f'{{backup_id="{self._label(str(latest_validation.get("backup_id") or ""))}",'
                    f'status="{self._label(str(latest_validation.get("status") or "unknown"))}",'
                    "restore_plan_schema_version="
                    f'"{restore_plan_schema}"}} '
                    f"{1 if restore_validated else 0}",
                    "# HELP dialecticore_backup_latest_restore_validation_age_seconds "
                    "Age of the latest backup dry-run restore validation evidence.",
                    "# TYPE dialecticore_backup_latest_restore_validation_age_seconds gauge",
                    "dialecticore_backup_latest_restore_validation_age_seconds "
                    f"{float(latest_validation.get('validation_age_seconds') or 0):.3f}",
                    "# HELP dialecticore_backup_latest_content_validation "
                    "Latest backup dry-run content validation evidence by scope.",
                    "# TYPE dialecticore_backup_latest_content_validation gauge",
                    self._backup_content_validation_metric(
                        "object_storage",
                        object_storage_validation,
                    ),
                    self._backup_content_validation_metric(
                        "runtime_state",
                        runtime_state_validation,
                    ),
                ]
            )
        auth_runtime = self._component(health, "auth_runtime")
        if auth_runtime is not None:
            auth_details = auth_runtime.get("details") or {}
            revocations = auth_details.get("provider_session_revocations") or {}
            decisions = auth_details.get("provider_session_decisions") or {}
            auth_enabled = auth_details.get("auth_enabled") is True
            api_key_enabled = auth_enabled and (
                auth_details.get("api_key_reference_configured") is True
            )
            trusted_identity_enabled = auth_enabled and (
                auth_details.get("trusted_identity_enabled") is True
            )
            provider_session_enabled = auth_enabled and (
                auth_details.get("provider_session_enabled") is True
            )
            lines.extend(
                [
                    "# HELP dialecticore_auth_mode_enabled Authentication mode readiness.",
                    "# TYPE dialecticore_auth_mode_enabled gauge",
                    'dialecticore_auth_mode_enabled{mode="api_key"} '
                    f"{1 if api_key_enabled else 0}",
                    'dialecticore_auth_mode_enabled{mode="trusted_identity"} '
                    f"{1 if trusted_identity_enabled else 0}",
                    'dialecticore_auth_mode_enabled{mode="provider_session"} '
                    f"{1 if provider_session_enabled else 0}",
                    "# HELP dialecticore_auth_provider_session_count "
                    "Provider session revocation and decision-log counters.",
                    "# TYPE dialecticore_auth_provider_session_count gauge",
                    'dialecticore_auth_provider_session_count{kind="active_revocations"} '
                    f"{int(revocations.get('active_count') or 0)}",
                    'dialecticore_auth_provider_session_count{kind="expired_revocations"} '
                    f"{int(revocations.get('expired_count') or 0)}",
                    'dialecticore_auth_provider_session_count{kind="total_revocations"} '
                    f"{int(revocations.get('total_count') or 0)}",
                    'dialecticore_auth_provider_session_count{kind="retained_decisions"} '
                    f"{int(decisions.get('retained_count') or 0)}",
                    'dialecticore_auth_provider_session_count{kind="accepted_decisions"} '
                    f"{int(decisions.get('accepted_count') or 0)}",
                    'dialecticore_auth_provider_session_count{kind="denied_decisions"} '
                    f"{int(decisions.get('denied_count') or 0)}",
                    'dialecticore_auth_provider_session_count{kind="error_decisions"} '
                    f"{int(decisions.get('error_count') or 0)}",
                ]
            )
        temporal = self._component(health, "temporal_runtime")
        if temporal is not None:
            details = temporal.get("details") or {}
            execution = details.get("temporal_worker_execution") or {}
            lines.extend(
                [
                    "# HELP dialecticore_temporal_runtime_status Temporal runtime mode status.",
                    "# TYPE dialecticore_temporal_runtime_status gauge",
                    "dialecticore_temporal_runtime_status"
                    f'{{mode="{self._label(str(details.get("mode", "unknown")))}",'
                    f'status="{self._label(str(temporal.get("status", "unknown")))}",'
                    f'namespace="{self._label(str(details.get("namespace", "default")))}",'
                    f'task_queue="{self._label(str(details.get("task_queue") or ""))}",'
                    "native_worker_enabled="
                    f'"{str(bool(details.get("native_worker_enabled"))).lower()}"}} 1',
                    "# HELP dialecticore_temporal_worker_execution_status "
                    "External Temporal worker execution evidence status.",
                    "# TYPE dialecticore_temporal_worker_execution_status gauge",
                    "dialecticore_temporal_worker_execution_status"
                    f'{{status="{self._label(str(execution.get("status", "unknown")))}",'
                    f'namespace="{self._label(str(details.get("namespace", "default")))}",'
                    f'task_queue="{self._label(str(details.get("task_queue") or ""))}",'
                    f'worker_id="{self._label(str(execution.get("worker_id") or ""))}"}} 1',
                    "# HELP dialecticore_temporal_worker_execution_count "
                    "External Temporal worker execution evidence counters.",
                    "# TYPE dialecticore_temporal_worker_execution_count gauge",
                    'dialecticore_temporal_worker_execution_count{kind="progressed_stages"} '
                    f"{int(execution.get('progressed_stage_count') or 0)}",
                    'dialecticore_temporal_worker_execution_count{kind="errors"} '
                    f"{int(execution.get('error_count') or 0)}",
                    'dialecticore_temporal_worker_execution_count{kind="activities"} '
                    f"{int(execution.get('activity_count') or 0)}",
                ]
            )
        model_generation = self._component(health, "model_generation_observability")
        if model_generation is not None:
            details = model_generation.get("details") or {}
            by_provider_type = details.get("by_provider_type") or {}
            lines.extend(
                [
                    "# HELP dialecticore_model_generation_turn_count "
                    "Persisted discussion turns with model-generation metadata.",
                    "# TYPE dialecticore_model_generation_turn_count gauge",
                    "dialecticore_model_generation_turn_count "
                    f"{int(details.get('turn_count') or 0)}",
                    "# HELP dialecticore_model_generation_latency_ms "
                    "Model-generation latency from persisted discussion-turn metadata.",
                    "# TYPE dialecticore_model_generation_latency_ms summary",
                    "dialecticore_model_generation_latency_ms_sum "
                    f"{float(details.get('model_latency_sum_ms') or 0):.3f}",
                    "dialecticore_model_generation_latency_ms_count "
                    f"{int(details.get('latency_recorded_turn_count') or 0)}",
                    "# HELP dialecticore_model_generation_token_usage_records "
                    "Persisted discussion turns with provider token-usage metadata.",
                    "# TYPE dialecticore_model_generation_token_usage_records gauge",
                    "dialecticore_model_generation_token_usage_records "
                    f"{int(details.get('token_usage_recorded_turn_count') or 0)}",
                    "# HELP dialecticore_model_generation_token_count "
                    "Aggregated provider token usage by token kind.",
                    "# TYPE dialecticore_model_generation_token_count counter",
                    'dialecticore_model_generation_token_count{kind="prompt"} '
                    f"{int(details.get('total_prompt_tokens') or 0)}",
                    'dialecticore_model_generation_token_count{kind="completion"} '
                    f"{int(details.get('total_completion_tokens') or 0)}",
                    'dialecticore_model_generation_token_count{kind="total"} '
                    f"{int(details.get('total_tokens') or 0)}",
                    "# HELP dialecticore_model_generation_provider_turn_count "
                    "Persisted discussion turns by model provider type.",
                    "# TYPE dialecticore_model_generation_provider_turn_count gauge",
                ]
            )
            for provider_type, provider_details in sorted(by_provider_type.items()):
                if not isinstance(provider_details, dict):
                    continue
                provider_label = self._label(str(provider_type))
                lines.append(
                    "dialecticore_model_generation_provider_turn_count"
                    f'{{provider_type="{provider_label}"}} '
                    f"{int(provider_details.get('turn_count') or 0)}"
                )
            lines.extend(
                [
                    "# HELP dialecticore_model_generation_provider_latency_ms "
                    "Model-generation latency by model provider type.",
                    "# TYPE dialecticore_model_generation_provider_latency_ms summary",
                ]
            )
            for provider_type, provider_details in sorted(by_provider_type.items()):
                if not isinstance(provider_details, dict):
                    continue
                provider_label = self._label(str(provider_type))
                lines.append(
                    "dialecticore_model_generation_provider_latency_ms_sum"
                    f'{{provider_type="{provider_label}"}} '
                    f"{float(provider_details.get('latency_sum_ms') or 0):.3f}"
                )
                lines.append(
                    "dialecticore_model_generation_provider_latency_ms_count"
                    f'{{provider_type="{provider_label}"}} '
                    f"{int(provider_details.get('latency_recorded_turn_count') or 0)}"
                )
            lines.extend(
                [
                    "# HELP dialecticore_model_generation_provider_token_count "
                    "Aggregated provider token usage by provider type and token kind.",
                    "# TYPE dialecticore_model_generation_provider_token_count counter",
                ]
            )
            for provider_type, provider_details in sorted(by_provider_type.items()):
                if not isinstance(provider_details, dict):
                    continue
                provider_label = self._label(str(provider_type))
                for kind, value_key in {
                    "prompt": "prompt_tokens",
                    "completion": "completion_tokens",
                    "total": "total_tokens",
                }.items():
                    lines.append(
                        "dialecticore_model_generation_provider_token_count"
                        f'{{provider_type="{provider_label}",kind="{kind}"}} '
                        f"{int(provider_details.get(value_key) or 0)}"
                    )

        lines.extend(
            [
                "# HELP dialecticore_episode_count Episode and configuration counters.",
                "# TYPE dialecticore_episode_count gauge",
            ]
        )
        for name, value in sorted((health.get("counts") or {}).items()):
            lines.append(f'dialecticore_episode_count{{kind="{self._label(name)}"}} {int(value)}')
        settings = health.get("settings") or {}
        lines.extend(
            [
                "# HELP dialecticore_publisher_automated_live_enabled "
                "Automated live publishing policy.",
                "# TYPE dialecticore_publisher_automated_live_enabled gauge",
                "dialecticore_publisher_automated_live_enabled "
                f"{1 if settings.get('publisher_automated_live_enabled') is True else 0}",
            ]
        )
        publisher_targets = self._component(health, "publisher_targets")
        if publisher_targets is not None:
            target_details = publisher_targets.get("details") or {}
            target_status = self._label(str(publisher_targets.get("status", "unknown")))
            lines.extend(
                [
                    "# HELP dialecticore_publisher_target_health_status "
                    "Publisher target health component status.",
                    "# TYPE dialecticore_publisher_target_health_status gauge",
                    f'dialecticore_publisher_target_health_status{{status="{target_status}"}} 1',
                    "# HELP dialecticore_publisher_target_count "
                    "Publisher target readiness and capability counters.",
                    "# TYPE dialecticore_publisher_target_count gauge",
                    'dialecticore_publisher_target_count{kind="configured"} '
                    f"{int(target_details.get('configured') or 0)}",
                    'dialecticore_publisher_target_count{kind="enabled"} '
                    f"{int(target_details.get('enabled') or 0)}",
                    'dialecticore_publisher_target_count{kind="live_enabled"} '
                    f"{int(target_details.get('live_enabled') or 0)}",
                    "dialecticore_publisher_target_count"
                    '{kind="automated_live_capable_enabled"} '
                    f"{int(target_details.get('automated_live_capable_enabled') or 0)}",
                    'dialecticore_publisher_target_count{kind="mock_enabled"} '
                    f"{int(target_details.get('mock_enabled') or 0)}",
                    'dialecticore_publisher_target_count{kind="dry_run_only_enabled"} '
                    f"{int(target_details.get('dry_run_only_enabled') or 0)}",
                    'dialecticore_publisher_target_count{kind="healthy"} '
                    f"{int(target_details.get('healthy') or 0)}",
                    'dialecticore_publisher_target_count{kind="unknown"} '
                    f"{int(target_details.get('unknown') or 0)}",
                    'dialecticore_publisher_target_count{kind="unhealthy"} '
                    f"{int(target_details.get('unhealthy') or 0)}",
                    'dialecticore_publisher_target_count{kind="issues"} '
                    f"{int(target_details.get('issue_count') or 0)}",
                ]
            )
        production_runs = self._component(health, "production_runs")
        if production_runs is not None:
            run_details = production_runs.get("details") or {}
            run_status = self._label(str(production_runs.get("status", "unknown")))
            lines.extend(
                [
                    "# HELP dialecticore_production_run_health_status "
                    "Production run health component status.",
                    "# TYPE dialecticore_production_run_health_status gauge",
                    f'dialecticore_production_run_health_status{{status="{run_status}"}} 1',
                    "# HELP dialecticore_production_run_count "
                    "Production run state counters from durable workflow control.",
                    "# TYPE dialecticore_production_run_count gauge",
                    'dialecticore_production_run_count{kind="total"} '
                    f"{int(run_details.get('production_run_count') or 0)}",
                    'dialecticore_production_run_count{kind="active"} '
                    f"{int(run_details.get('active_production_runs') or 0)}",
                    'dialecticore_production_run_count{kind="running_active"} '
                    f"{int(run_details.get('running_active_production_runs') or 0)}",
                    'dialecticore_production_run_count{kind="paused_active"} '
                    f"{int(run_details.get('paused_active_production_runs') or 0)}",
                    'dialecticore_production_run_count{kind="failed_active"} '
                    f"{int(run_details.get('failed_active_production_runs') or 0)}",
                    'dialecticore_production_run_count{kind="cancelled_active"} '
                    f"{int(run_details.get('cancelled_active_production_runs') or 0)}",
                    'dialecticore_production_run_count{kind="completion_blocked"} '
                    f"{int(run_details.get('completion_blocked_production_runs') or 0)}",
                    'dialecticore_production_run_count{kind="attention"} '
                    f"{int(run_details.get('attention_count') or 0)}",
                ]
            )
            for failed_check, count in sorted(
                (run_details.get("by_completion_failed_check") or {}).items()
            ):
                lines.append(
                    "dialecticore_production_run_count"
                    f'{{kind="completion_failed_check",'
                    f'check="{self._label(str(failed_check))}"}} {int(count)}'
                )
        workflow_duration = self._component(health, "workflow_duration_observability")
        if workflow_duration is not None:
            duration_details = workflow_duration.get("details") or {}
            by_stage = duration_details.get("by_stage") or {}
            by_language = duration_details.get("by_language") or {}
            lines.extend(
                [
                    "# HELP dialecticore_episode_production_duration_ms "
                    "Workflow production duration from durable run timestamps.",
                    "# TYPE dialecticore_episode_production_duration_ms summary",
                    "dialecticore_episode_production_duration_ms_sum "
                    f"{int(duration_details.get('production_duration_ms_sum') or 0)}",
                    "dialecticore_episode_production_duration_ms_count "
                    f"{int(duration_details.get('production_duration_record_count') or 0)}",
                    "# HELP dialecticore_workflow_stage_duration_ms "
                    "Workflow stage duration from durable stage history.",
                    "# TYPE dialecticore_workflow_stage_duration_ms summary",
                    "dialecticore_workflow_stage_duration_ms_sum "
                    f"{int(duration_details.get('stage_duration_ms_sum') or 0)}",
                    "dialecticore_workflow_stage_duration_ms_count "
                    f"{int(duration_details.get('stage_duration_record_count') or 0)}",
                    "# HELP dialecticore_language_production_duration_ms "
                    "Per-language production span from persisted asset timestamps.",
                    "# TYPE dialecticore_language_production_duration_ms summary",
                ]
            )
            for stage, stage_details in sorted(by_stage.items()):
                if not isinstance(stage_details, dict):
                    continue
                stage_label = self._label(str(stage))
                lines.append(
                    "dialecticore_workflow_stage_duration_ms_sum"
                    f'{{stage="{stage_label}"}} '
                    f"{int(stage_details.get('duration_ms_sum') or 0)}"
                )
                lines.append(
                    "dialecticore_workflow_stage_duration_ms_count"
                    f'{{stage="{stage_label}"}} '
                    f"{int(stage_details.get('duration_record_count') or 0)}"
                )
            for language, language_details in sorted(by_language.items()):
                if not isinstance(language_details, dict):
                    continue
                language_label = self._label(str(language))
                lines.append(
                    "dialecticore_language_production_duration_ms_sum"
                    f'{{language="{language_label}"}} '
                    f"{int(language_details.get('duration_ms_sum') or 0)}"
                )
                lines.append(
                    "dialecticore_language_production_duration_ms_count"
                    f'{{language="{language_label}"}} '
                    f"{int(language_details.get('duration_record_count') or 0)}"
                )
        publish_job_counts = self._publish_job_counts(health)
        lines.extend(
            [
                "# HELP dialecticore_publish_job_count Persisted publish job counters.",
                "# TYPE dialecticore_publish_job_count gauge",
            ]
        )
        for name, value in publish_job_counts.items():
            lines.append(f'dialecticore_publish_job_count{{kind="{name}"}} {value}')
        publish_package_manifest_counts = self._publish_package_manifest_counts(health)
        lines.extend(
            [
                "# HELP dialecticore_publish_package_manifest_count "
                "Delivery package and production manifest counters.",
                "# TYPE dialecticore_publish_package_manifest_count gauge",
            ]
        )
        for name, value in publish_package_manifest_counts.items():
            lines.append(f'dialecticore_publish_package_manifest_count{{kind="{name}"}} {value}')

        workflow_orchestration = self._component(health, "workflow_orchestration")
        if workflow_orchestration is not None:
            orchestration_details = workflow_orchestration.get("details") or {}
            lines.extend(
                [
                    "# HELP dialecticore_workflow_orchestration_count "
                    "Workflow-worker orchestration and Temporal dispatch counters.",
                    "# TYPE dialecticore_workflow_orchestration_count gauge",
                    'dialecticore_workflow_orchestration_count{dimension="attempts",value="all"} '
                    f"{int(orchestration_details.get('attempt_count') or 0)}",
                    "dialecticore_workflow_orchestration_count"
                    '{dimension="stages",value="progressed"} '
                    f"{int(orchestration_details.get('progressed_stage_count') or 0)}",
                    'dialecticore_workflow_orchestration_count{dimension="stages",value="failed"} '
                    f"{int(orchestration_details.get('failed_stage_count') or 0)}",
                    'dialecticore_workflow_orchestration_count{dimension="errors",value="all"} '
                    f"{int(orchestration_details.get('error_count') or 0)}",
                    'dialecticore_workflow_orchestration_count{dimension="dispatch",value="all"} '
                    f"{int(orchestration_details.get('dispatch_count') or 0)}",
                    "dialecticore_workflow_orchestration_count"
                    '{dimension="dispatch",value="blocked"} '
                    f"{int(orchestration_details.get('blocked_dispatch_count') or 0)}",
                    'dialecticore_workflow_orchestration_count{dimension="dispatch",value="ready"} '
                    f"{int(orchestration_details.get('ready_dispatch_count') or 0)}",
                    "dialecticore_workflow_orchestration_count"
                    '{dimension="production_handoff",value="all"} '
                    f"{int(orchestration_details.get('production_handoff_count') or 0)}",
                    "dialecticore_workflow_orchestration_count"
                    '{dimension="production_handoff",value="blocked"} '
                    f"{int(orchestration_details.get('blocked_production_handoff_count') or 0)}",
                    "dialecticore_workflow_orchestration_count"
                    '{dimension="production_handoff",value="review_ready"} '
                    f"{int(orchestration_details.get(
                        'review_ready_production_handoff_count'
                    ) or 0)}",
                    "dialecticore_workflow_orchestration_count"
                    '{dimension="production_handoff",value="delivery_ready"} '
                    f"{int(orchestration_details.get(
                        'delivery_ready_production_handoff_count'
                    ) or 0)}",
                ]
            )
            for dimension, counts in {
                "worker": orchestration_details.get("by_worker") or {},
                "policy": orchestration_details.get("by_policy") or {},
                "dispatch_status": orchestration_details.get("by_dispatch_status") or {},
                "failed_stage": orchestration_details.get("by_failed_stage") or {},
                "progressed_stage": orchestration_details.get("by_progressed_stage") or {},
                "blocked_dispatch_stage": orchestration_details.get("by_blocked_dispatch_stage")
                or {},
                "ready_dispatch_stage": orchestration_details.get("by_ready_dispatch_stage") or {},
                "production_handoff_status": orchestration_details.get(
                    "by_production_handoff_status"
                )
                or {},
                "production_handoff_blocker": orchestration_details.get(
                    "by_production_handoff_blocker"
                )
                or {},
            }.items():
                for value, count in sorted(counts.items()):
                    lines.append(
                        "dialecticore_workflow_orchestration_count"
                        f'{{dimension="{self._label(str(dimension))}",'
                        f'value="{self._label(str(value))}"}} {int(count)}'
                    )

        workflow_retries = self._component(health, "workflow_retries")
        if workflow_retries is not None:
            retry_details = workflow_retries.get("details") or {}
            lines.extend(
                [
                    "# HELP dialecticore_workflow_stage_retry_count "
                    "Workflow stage retry backlog and resolved-history counters.",
                    "# TYPE dialecticore_workflow_stage_retry_count gauge",
                    'dialecticore_workflow_stage_retry_count{dimension="total",value="all"} '
                    f"{int(retry_details.get('total_retry_entries') or 0)}",
                    'dialecticore_workflow_stage_retry_count{dimension="history",value="all"} '
                    f"{int(retry_details.get('historical_retry_entries') or 0)}",
                    'dialecticore_workflow_stage_retry_count{dimension="history_status",'
                    'value="resolved"} '
                    f"{int(retry_details.get('resolved_retry_entries') or 0)}",
                ]
            )
            for dimension, counts in {
                "status": retry_details.get("by_status") or {},
                "stage": retry_details.get("by_stage") or {},
                "schedule_status": retry_details.get("by_schedule_status") or {},
                "due_stage": retry_details.get("by_due_stage") or {},
                "backoff_stage": retry_details.get("by_backoff_stage") or {},
                "unknown_schedule_stage": retry_details.get("by_unknown_schedule_stage") or {},
                "exhausted_stage": retry_details.get("by_exhausted_stage") or {},
                "resolution_status": retry_details.get("by_resolution_status") or {},
                "resolution_stage": retry_details.get("by_resolution_stage") or {},
            }.items():
                for value, count in sorted(counts.items()):
                    lines.append(
                        "dialecticore_workflow_stage_retry_count"
                        f'{{dimension="{self._label(str(dimension))}",'
                        f'value="{self._label(str(value))}"}} {int(count)}'
                    )

        lines.extend(
            [
                "# HELP dialecticore_queue_count Pending and failed media queue counters.",
                "# TYPE dialecticore_queue_count gauge",
            ]
        )
        for name, value in sorted((health.get("queues") or {}).items()):
            lines.append(f'dialecticore_queue_count{{kind="{self._label(name)}"}} {int(value)}')

        queue_wait = self._component(health, "queue_wait_observability")
        if queue_wait is not None:
            details = queue_wait.get("details") or {}
            lines.extend(
                [
                    "# HELP dialecticore_queue_wait_duration_ms "
                    "Queue wait age or submitted-to-completed span from persisted timestamps.",
                    "# TYPE dialecticore_queue_wait_duration_ms summary",
                    'dialecticore_queue_wait_duration_ms_sum{state="pending"} '
                    f"{int(details.get('pending_wait_ms_sum') or 0)}",
                    'dialecticore_queue_wait_duration_ms_count{state="pending"} '
                    f"{int(details.get('pending_wait_record_count') or 0)}",
                    'dialecticore_queue_wait_duration_ms_sum{state="completed"} '
                    f"{int(details.get('completed_wait_ms_sum') or 0)}",
                    'dialecticore_queue_wait_duration_ms_count{state="completed"} '
                    f"{int(details.get('completed_wait_record_count') or 0)}",
                ]
            )
            for dimension, groups in {
                "queue": details.get("by_queue") or {},
                "language": details.get("by_language") or {},
            }.items():
                for value, group_details in sorted(groups.items()):
                    if not isinstance(group_details, dict):
                        continue
                    labels = (
                        f'dimension="{self._label(dimension)}",value="{self._label(str(value))}"'
                    )
                    for state, sum_key, count_key in (
                        (
                            "pending",
                            "pending_wait_ms_sum",
                            "pending_wait_record_count",
                        ),
                        (
                            "completed",
                            "completed_wait_ms_sum",
                            "completed_wait_record_count",
                        ),
                    ):
                        lines.append(
                            "dialecticore_queue_wait_duration_ms_sum"
                            f'{{{labels},state="{state}"}} '
                            f"{int(group_details.get(sum_key) or 0)}"
                        )
                        lines.append(
                            "dialecticore_queue_wait_duration_ms_count"
                            f'{{{labels},state="{state}"}} '
                            f"{int(group_details.get(count_key) or 0)}"
                        )

        asset_observability = self._component(health, "asset_production_observability")
        if asset_observability is not None:
            details = asset_observability.get("details") or {}
            by_asset_type = details.get("by_asset_type") or {}
            by_language = details.get("by_language") or {}
            lines.extend(
                [
                    "# HELP dialecticore_production_asset_count "
                    "Production asset counts by status, type, and language.",
                    "# TYPE dialecticore_production_asset_count gauge",
                    'dialecticore_production_asset_count{dimension="status",value="all"} '
                    f"{int(details.get('asset_count') or 0)}",
                    'dialecticore_production_asset_count{dimension="status",value="completed"} '
                    f"{int(details.get('completed_asset_count') or 0)}",
                    'dialecticore_production_asset_count{dimension="status",value="failed"} '
                    f"{int(details.get('failed_asset_count') or 0)}",
                    "# HELP dialecticore_production_asset_failure_rate "
                    "Production asset failure ratio by aggregate dimension.",
                    "# TYPE dialecticore_production_asset_failure_rate gauge",
                    'dialecticore_production_asset_failure_rate{dimension="all",value="all"} '
                    f"{float(details.get('failure_rate') or 0):.6f}",
                    "# HELP dialecticore_production_asset_duration_ms "
                    "Persisted production asset duration by aggregate dimension.",
                    "# TYPE dialecticore_production_asset_duration_ms summary",
                    'dialecticore_production_asset_duration_ms_sum{dimension="all",value="all"} '
                    f"{int(details.get('duration_sum_ms') or 0)}",
                    'dialecticore_production_asset_duration_ms_count{dimension="all",value="all"} '
                    f"{int(details.get('duration_recorded_asset_count') or 0)}",
                    "# HELP dialecticore_production_asset_storage_size_bytes "
                    "Persisted or locally probed production asset storage size "
                    "by aggregate dimension.",
                    "# TYPE dialecticore_production_asset_storage_size_bytes gauge",
                    'dialecticore_production_asset_storage_size_bytes{dimension="all",value="all"} '
                    f"{int(details.get('storage_size_bytes') or 0)}",
                ]
            )
            for dimension, groups in {
                "asset_type": by_asset_type,
                "language": by_language,
            }.items():
                for value, group_details in sorted(groups.items()):
                    if not isinstance(group_details, dict):
                        continue
                    labels = (
                        f'dimension="{self._label(dimension)}",value="{self._label(str(value))}"'
                    )
                    lines.append(
                        f"dialecticore_production_asset_count{{{labels}}} "
                        f"{int(group_details.get('asset_count') or 0)}"
                    )
                    lines.append(
                        f"dialecticore_production_asset_failure_rate{{{labels}}} "
                        f"{float(group_details.get('failure_rate') or 0):.6f}"
                    )
                    lines.append(
                        f"dialecticore_production_asset_duration_ms_sum{{{labels}}} "
                        f"{int(group_details.get('duration_sum_ms') or 0)}"
                    )
                    lines.append(
                        f"dialecticore_production_asset_duration_ms_count{{{labels}}} "
                        f"{int(group_details.get('duration_recorded_asset_count') or 0)}"
                    )
                    lines.append(
                        f"dialecticore_production_asset_storage_size_bytes{{{labels}}} "
                        f"{int(group_details.get('storage_size_bytes') or 0)}"
                    )

        lines.extend(
            [
                "# HELP dialecticore_worker_count Worker liveness counters.",
                "# TYPE dialecticore_worker_count gauge",
            ]
        )
        for name, value in sorted(workers.counts.items()):
            lines.append(f'dialecticore_worker_count{{kind="{self._label(name)}"}} {int(value)}')

        lines.extend(
            [
                "# HELP dialecticore_worker_runtime_seconds "
                "Worker runtime TTL and retention settings.",
                "# TYPE dialecticore_worker_runtime_seconds gauge",
                'dialecticore_worker_runtime_seconds{kind="heartbeat_ttl"} '
                f"{int(workers.heartbeat_ttl_seconds)}",
                'dialecticore_worker_runtime_seconds{kind="lease_ttl"} '
                f"{int(workers.lease_ttl_seconds)}",
                'dialecticore_worker_runtime_seconds{kind="runtime_state_retention"} '
                f"{int(workers.runtime_state_retention_seconds)}",
            ]
        )

        signal_summary = health.get("worker_signals") or {}
        lines.extend(
            [
                "# HELP dialecticore_worker_signal_count Worker control signal counters.",
                "# TYPE dialecticore_worker_signal_count gauge",
                'dialecticore_worker_signal_count{dimension="recent",value="all"} '
                f"{int(signal_summary.get('recent_count') or 0)}",
                'dialecticore_worker_signal_count{dimension="blocking",value="all"} '
                f"{int(signal_summary.get('blocking_count') or 0)}",
                'dialecticore_worker_signal_count{dimension="failed",value="all"} '
                f"{int(signal_summary.get('failed_count') or 0)}",
                'dialecticore_worker_signal_count{dimension="malformed",value="all"} '
                f"{int(signal_summary.get('malformed_count') or 0)}",
            ]
        )
        for dimension, counts in {
            "status": signal_summary.get("by_status") or {},
            "signal_type": signal_summary.get("by_signal_type") or {},
            "target_role": signal_summary.get("by_target_role") or {},
            "active_blocking_target_role": signal_summary.get("by_active_blocking_target_role")
            or {},
            "delivery_source": signal_summary.get("by_delivery_source") or {},
        }.items():
            for value, count in sorted(counts.items()):
                lines.append(
                    "dialecticore_worker_signal_count"
                    f'{{dimension="{self._label(str(dimension))}",'
                    f'value="{self._label(str(value))}"}} {int(count)}'
                )

        lines.extend(
            [
                "# HELP dialecticore_worker_heartbeat_age_seconds Last heartbeat age by worker.",
                "# TYPE dialecticore_worker_heartbeat_age_seconds gauge",
            ]
        )
        for worker in workers.workers:
            stale = "true" if worker.stale else "false"
            lines.append(
                "dialecticore_worker_heartbeat_age_seconds"
                f'{{role="{self._label(worker.role)}",'
                f'worker_id="{self._label(worker.worker_id)}",'
                f'status="{self._label(worker.status)}",stale="{stale}"}} '
                f"{worker.heartbeat_age_seconds:.3f}"
            )
        lines.extend(
            [
                "# HELP dialecticore_worker_lease_expires_in_seconds Worker role lease expiry.",
                "# TYPE dialecticore_worker_lease_expires_in_seconds gauge",
            ]
        )
        for lease in workers.leases:
            expired = "true" if lease.expired else "false"
            lines.append(
                "dialecticore_worker_lease_expires_in_seconds"
                f'{{role="{self._label(lease.role)}",'
                f'worker_id="{self._label(lease.worker_id)}",expired="{expired}"}} '
                f"{lease.expires_in_seconds:.3f}"
            )
        lines.append("")
        return "\n".join(lines)

    def _component(self, health: dict, name: str) -> dict | None:
        for component in health.get("components", []):
            if component.get("name") == name:
                return component
        return None

    def _publish_job_counts(self, health: dict) -> dict[str, int]:
        counts = health.get("counts") or {}
        return {
            "total": int(counts.get("publish_jobs") or 0),
            "submitted": int(counts.get("submitted_publish_jobs") or 0),
            "completed": int(counts.get("completed_publish_jobs") or 0),
            "failed": int(counts.get("failed_publish_jobs") or 0),
            "dry_run": int(counts.get("dry_run_publish_jobs") or 0),
            "live": int(counts.get("live_publish_jobs") or 0),
        }

    def _publish_package_manifest_counts(self, health: dict) -> dict[str, int]:
        counts = health.get("counts") or {}
        return {
            "completed_export_packages": int(counts.get("completed_export_packages") or 0),
            "production_manifest_assets": int(counts.get("production_manifest_assets") or 0),
            "invalid_production_manifest_assets": int(
                counts.get("invalid_production_manifest_assets") or 0
            ),
            "packages_missing_package_qc": int(counts.get("packages_missing_package_qc") or 0),
            "packages_failing_package_qc": int(counts.get("packages_failing_package_qc") or 0),
            "packages_missing_thumbnail": int(counts.get("packages_missing_thumbnail") or 0),
            "packages_missing_subtitles": int(counts.get("packages_missing_subtitles") or 0),
            "packages_missing_production_manifest": int(
                counts.get("packages_missing_production_manifest") or 0
            ),
        }

    def _label(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _backup_content_validation_metric(self, scope: str, validation: dict) -> str:
        validated = validation.get("validated") is True
        return (
            "dialecticore_backup_latest_content_validation"
            f'{{scope="{self._label(scope)}",'
            f'status="{self._label(str(validation.get("status") or "missing"))}",'
            "schema_version="
            f'"{self._label(str(validation.get("schema_version") or ""))}"}} '
            f"{1 if validated else 0}"
        )
