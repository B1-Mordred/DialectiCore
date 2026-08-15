from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.core.config import Settings
from app.core.credentials import (
    credential_reference_scheme,
    public_credential_reference,
    public_credential_target,
)
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity
from app.domain.schemas import (
    Asset,
    AuditEvent,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    Episode,
    LanguageProfile,
    ModelEndpoint,
    ParticipantProfile,
    Project,
    PublisherTarget,
    PublishJob,
    QualityResult,
    VisualProfile,
    VoiceboxEndpoint,
    VoiceProfile,
    WorkerStatusSummary,
)
from app.services.auth_service import ROLE_PERMISSIONS
from app.services.managed_media_smoke_evidence import managed_media_smoke_evidence
from app.services.model_gateway import SecretResolver
from app.services.worker_status_service import configured_worker_roles
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

BACKUP_HEALTH_CHECKSUM_MAX_BYTES = 64 * 1024 * 1024


class SystemHealthRepository(Protocol):
    def list_projects(self) -> list[Project]: ...

    def list_language_profiles(self) -> list[LanguageProfile]: ...

    def list(self) -> list[Episode]: ...

    def list_model_endpoints(self) -> list[ModelEndpoint]: ...

    def list_participant_profiles(self) -> list[ParticipantProfile]: ...

    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]: ...

    def list_voice_profiles(self) -> list[VoiceProfile]: ...

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]: ...

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]: ...

    def list_visual_profiles(self) -> list[VisualProfile]: ...

    def list_publisher_targets(self) -> list[PublisherTarget]: ...

    def list_audit_events(
        self,
        limit: int = 50,
        event_type: str | None = None,
    ) -> list[AuditEvent]: ...


class SystemHealthService:
    def __init__(
        self,
        settings: Settings,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.settings = settings
        self.secret_resolver = secret_resolver or SecretResolver()

    def summary(
        self,
        repository: SystemHealthRepository,
        worker_status: WorkerStatusSummary | None = None,
        worker_signal_summary: dict | None = None,
    ) -> dict:
        component_checks: list[dict] = []
        try:
            projects = repository.list_projects()
            language_profiles = repository.list_language_profiles()
            episodes = self._status_episodes(repository)
            model_endpoints = repository.list_model_endpoints()
            participant_profiles = repository.list_participant_profiles()
            voicebox_endpoints = repository.list_voicebox_endpoints()
            voice_profiles = repository.list_voice_profiles()
            comfyui_endpoints = repository.list_comfyui_endpoints()
            comfyui_workflows = repository.list_comfyui_workflows()
            visual_profiles = repository.list_visual_profiles()
            publisher_targets = repository.list_publisher_targets()
            audit_events = self._health_audit_events(repository)
        except Exception as exc:
            readiness_checks = {"database_reachable": False}
            return {
                "status": "unhealthy",
                "service": "production-api",
                "checked_at": datetime.now(UTC).isoformat(),
                "components": [
                    {
                        "name": "database",
                        "status": "unhealthy",
                        "details": {
                            "error": type(exc).__name__,
                            "message": str(exc),
                            "readiness_checks": readiness_checks,
                            "failed_readiness_checks": [
                                name for name, ready in readiness_checks.items() if not ready
                            ],
                            "reason": "database repository could not be queried",
                        },
                    }
                ],
                "counts": {},
                "queues": {},
                "settings": self._settings_summary(),
            }

        component_checks.append(
            {
                "name": "database",
                "status": "healthy",
                "details": {
                    "episode_count": len(episodes),
                    "readiness_checks": {"database_reachable": True},
                    "failed_readiness_checks": [],
                    "reason": "database repository queries completed",
                },
            }
        )
        component_checks.append(self._database_migrations_check())
        runtime_paths = self._runtime_paths_check()
        component_checks.append(runtime_paths)
        component_checks.append(
            self._deployment_readiness_check(
                publisher_targets=publisher_targets,
                model_endpoints=model_endpoints,
                voicebox_endpoints=voicebox_endpoints,
                comfyui_endpoints=comfyui_endpoints,
            )
        )
        component_checks.append(
            self._credential_reference_check(
                model_endpoints=model_endpoints,
                voicebox_endpoints=voicebox_endpoints,
                comfyui_endpoints=comfyui_endpoints,
                publisher_targets=publisher_targets,
            )
        )
        component_checks.append(
            self._credential_provisioning_check(
                model_endpoints=model_endpoints,
                voicebox_endpoints=voicebox_endpoints,
                comfyui_endpoints=comfyui_endpoints,
                publisher_targets=publisher_targets,
            )
        )
        component_checks.append(self._redis_runtime_check())
        component_checks.append(self._auth_runtime_check())
        component_checks.append(self._object_storage_check())
        component_checks.append(self._backup_storage_check(audit_events))
        component_checks.extend(self._runtime_tool_checks())
        component_checks.append(self._temporal_runtime_check(worker_status))
        component_checks.append(
            {
                "name": "model_endpoints",
                "status": self._endpoint_collection_status(model_endpoints),
                "details": self._endpoint_collection_details(model_endpoints),
            }
        )
        component_checks.append(
            {
                "name": "voicebox_endpoints",
                "status": self._endpoint_collection_status(voicebox_endpoints),
                "details": self._endpoint_collection_details(voicebox_endpoints),
            }
        )
        component_checks.append(
            {
                "name": "comfyui_endpoints",
                "status": self._endpoint_collection_status(comfyui_endpoints),
                "details": self._endpoint_collection_details(comfyui_endpoints),
            }
        )
        component_checks.append(self._publisher_target_check(publisher_targets))
        production_run_summary = self._production_run_summary(episodes)
        component_checks.append(self._production_run_check(production_run_summary))
        workflow_duration_summary = self._workflow_duration_observability_summary(episodes)
        component_checks.append(
            self._workflow_duration_observability_check(workflow_duration_summary)
        )
        workflow_orchestration_summary = self._workflow_orchestration_summary(episodes)
        workflow_orchestration_details = self._workflow_orchestration_details_with_readiness(
            workflow_orchestration_summary
        )
        current_error_count = workflow_orchestration_summary["current_error_count"]
        current_blocked_dispatch_count = workflow_orchestration_summary[
            "current_blocked_dispatch_count"
        ]
        current_blocked_handoff_count = workflow_orchestration_summary[
            "current_blocked_production_handoff_count"
        ]
        component_checks.append(
            {
                "name": "workflow_orchestration",
                "status": (
                    "degraded"
                    if current_error_count > 0
                    or current_blocked_dispatch_count > 0
                    or current_blocked_handoff_count > 0
                    else "healthy"
                ),
                "details": workflow_orchestration_details,
            }
        )
        workflow_retry_summary = self._workflow_retry_summary(episodes)
        workflow_retry_details = self._workflow_retry_details_with_readiness(workflow_retry_summary)
        component_checks.append(
            {
                "name": "workflow_retries",
                "status": (
                    "degraded" if workflow_retry_summary["total_retry_entries"] > 0 else "healthy"
                ),
                "details": workflow_retry_details,
            }
        )
        model_generation_summary = self._model_generation_observability_summary(episodes)
        component_checks.append(
            self._model_generation_observability_check(model_generation_summary)
        )
        asset_observability_summary = self._asset_production_observability_summary(episodes)
        component_checks.append(
            self._asset_production_observability_check(asset_observability_summary)
        )
        queue_wait_summary = self._queue_wait_observability_summary(episodes)
        component_checks.append(self._queue_wait_observability_check(queue_wait_summary))
        if worker_status is not None:
            component_checks.append(self._worker_registry_check(worker_status))
        if worker_signal_summary is not None:
            component_checks.append(self._worker_signal_check(worker_signal_summary))

        counts = self._counts(
            projects=projects,
            language_profiles=language_profiles,
            episodes=episodes,
            model_endpoints=model_endpoints,
            participant_profiles=participant_profiles,
            voicebox_endpoints=voicebox_endpoints,
            voice_profiles=voice_profiles,
            comfyui_endpoints=comfyui_endpoints,
            comfyui_workflows=comfyui_workflows,
            visual_profiles=visual_profiles,
            publisher_targets=publisher_targets,
        )
        queues = self._queue_summary(episodes)
        if worker_status is not None:
            counts = counts | {
                "active_workers": worker_status.counts.get("active_workers", 0),
                "stale_workers": worker_status.counts.get("stale_workers", 0),
                "failed_workers": worker_status.counts.get("failed_workers", 0),
                "active_worker_roles": worker_status.counts.get("active_roles", 0),
                "active_worker_leases": worker_status.counts.get("active_leases", 0),
                "expired_worker_leases": worker_status.counts.get("expired_leases", 0),
            }
        if worker_signal_summary is not None:
            counts = counts | {
                "recent_worker_signals": int(worker_signal_summary.get("recent_count") or 0),
                "blocking_worker_signals": int(worker_signal_summary.get("blocking_count") or 0),
                "failed_worker_signals": int(worker_signal_summary.get("failed_count") or 0),
            }
        counts = counts | self._model_generation_observability_counts(model_generation_summary)
        counts = counts | self._asset_production_observability_counts(asset_observability_summary)
        counts = counts | self._workflow_duration_observability_counts(workflow_duration_summary)
        counts = counts | self._queue_wait_observability_counts(queue_wait_summary)
        overall = self._overall_status(component_checks, queues)
        return {
            "status": overall,
            "service": "production-api",
            "checked_at": datetime.now(UTC).isoformat(),
            "components": component_checks,
            "counts": counts,
            "queues": queues,
            "worker_signals": worker_signal_summary,
            "settings": self._settings_summary(),
        }

    def workflow_retry_backlog(
        self,
        repository: SystemHealthRepository,
        limit: int = 50,
    ) -> dict:
        episodes = self._status_episodes(repository)
        entries = sorted(
            self._workflow_retry_entries(episodes),
            key=self._workflow_retry_entry_sort_key,
        )
        return {
            "schema_version": "workflow_retry_backlog.v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "summary": self._workflow_retry_summary(episodes),
            "entries": entries[:limit],
            "limit": limit,
            "truncated": len(entries) > limit,
        }

    def workflow_orchestration_evidence(
        self,
        repository: SystemHealthRepository,
        limit: int = 50,
    ) -> dict:
        episodes = self._status_episodes(repository)
        attempts = sorted(
            self._workflow_orchestration_entries(episodes),
            key=lambda attempt: self._timestamp_sort_key(attempt.get("recorded_at")),
            reverse=True,
        )
        dispatches = sorted(
            self._temporal_dispatch_entries(episodes),
            key=lambda dispatch: self._timestamp_sort_key(dispatch.get("requested_at")),
            reverse=True,
        )
        return {
            "schema_version": "workflow_orchestration_evidence.v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "summary": self._workflow_orchestration_summary(episodes),
            "attempts": attempts[:limit],
            "dispatches": dispatches[:limit],
            "limit": limit,
            "truncated": len(attempts) > limit or len(dispatches) > limit,
        }

    def live_provider_readiness(
        self,
        repository: SystemHealthRepository,
        worker_status: WorkerStatusSummary | None = None,
        worker_signal_summary: dict | None = None,
    ) -> dict:
        try:
            model_endpoints = repository.list_model_endpoints()
            voicebox_endpoints = repository.list_voicebox_endpoints()
            comfyui_endpoints = repository.list_comfyui_endpoints()
            publisher_targets = repository.list_publisher_targets()
            audit_events = self._health_audit_events(repository)
            episodes = self._status_episodes(repository)
        except Exception as exc:
            return {
                "schema_version": "live_provider_readiness.v1",
                "status": "fail",
                "checked_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "check_count": 1,
                    "pass_count": 0,
                    "warning_count": 0,
                    "fail_count": 1,
                    "blocker_count": 1,
                },
                "checks": [
                    {
                        "category": "database",
                        "status": "fail",
                        "label": "Repository access",
                        "details": {"error": type(exc).__name__, "message": str(exc)},
                        "blockers": ["repository could not be queried for live readiness"],
                    }
                ],
                "blockers": ["repository could not be queried for live readiness"],
                "warnings": [],
            }

        checks = [
            self._readiness_check_from_component(
                self._deployment_readiness_check(
                    publisher_targets=publisher_targets,
                    model_endpoints=model_endpoints,
                    voicebox_endpoints=voicebox_endpoints,
                    comfyui_endpoints=comfyui_endpoints,
                )
            ),
            self._readiness_check_from_component(self._database_migrations_check()),
            self._readiness_check_from_component(self._runtime_paths_check()),
            self._readiness_check_from_component(
                self._credential_reference_check(
                    model_endpoints=model_endpoints,
                    voicebox_endpoints=voicebox_endpoints,
                    comfyui_endpoints=comfyui_endpoints,
                    publisher_targets=publisher_targets,
                )
            ),
            self._credential_provisioning_readiness(
                model_endpoints=model_endpoints,
                voicebox_endpoints=voicebox_endpoints,
                comfyui_endpoints=comfyui_endpoints,
                publisher_targets=publisher_targets,
            ),
            self._live_model_provider_readiness(model_endpoints),
            self._live_endpoint_readiness(
                "voicebox",
                "Voicebox endpoints",
                voicebox_endpoints,
                require_remote_base_url=True,
            ),
            self._live_endpoint_readiness(
                "comfyui",
                "ComfyUI endpoints",
                comfyui_endpoints,
                require_remote_base_url=True,
            ),
            self._managed_media_smoke_readiness(),
            self._readiness_check_from_component(self._object_storage_check()),
            self._readiness_check_from_component(self._backup_storage_check(audit_events)),
            self._readiness_check_from_component(self._redis_runtime_check()),
            self._readiness_check_from_component(self._auth_runtime_check()),
            self._readiness_check_from_component(self._temporal_runtime_check(worker_status)),
            self._readiness_check_from_component(self._worker_registry_check(worker_status)),
            self._worker_signal_readiness(worker_signal_summary),
            self._production_run_readiness(self._production_run_summary(episodes)),
            self._workflow_orchestration_readiness(self._workflow_orchestration_summary(episodes)),
            self._workflow_retry_readiness(self._workflow_retry_summary(episodes)),
            self._publish_job_readiness(self._publish_job_summary(episodes)),
            self._media_queue_readiness(self._queue_summary(episodes)),
            self._publisher_readiness(publisher_targets),
        ]
        blockers = [
            blocker
            for check in checks
            for blocker in check.get("blockers", [])
            if isinstance(blocker, str) and blocker
        ]
        warnings = [
            warning
            for check in checks
            for warning in check.get("warnings", [])
            if isinstance(warning, str) and warning
        ]
        fail_count = sum(1 for check in checks if check["status"] == "fail")
        warning_count = sum(1 for check in checks if check["status"] == "warning")
        pass_count = sum(1 for check in checks if check["status"] == "pass")
        return {
            "schema_version": "live_provider_readiness.v1",
            "status": "fail" if fail_count else "warning" if warning_count else "pass",
            "checked_at": datetime.now(UTC).isoformat(),
            "summary": {
                "check_count": len(checks),
                "pass_count": pass_count,
                "warning_count": warning_count,
                "fail_count": fail_count,
                "blocker_count": len(blockers),
                "warning_item_count": len(warnings),
            },
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
        }

    @staticmethod
    def _status_episodes(repository: SystemHealthRepository) -> list[Episode]:
        """Use the compact index when the repository provides one.

        Health and orchestration summaries only inspect state and asset metadata;
        they never need full word, phoneme, or viseme alignment arrays.
        """
        list_compact = getattr(repository, "list_compact", None)
        if callable(list_compact):
            return list(list_compact())
        return list(repository.list())

    def _health_audit_events(self, repository: SystemHealthRepository) -> list[AuditEvent]:
        recent_events = repository.list_audit_events(limit=200)
        backup_validation_events = repository.list_audit_events(
            limit=500,
            event_type="backup.restore_validated",
        )
        if not backup_validation_events:
            return recent_events
        by_id = {event.id: event for event in recent_events}
        for event in backup_validation_events:
            by_id.setdefault(event.id, event)
        return sorted(by_id.values(), key=lambda event: event.created_at, reverse=True)

    def episode_pilot_readiness(
        self,
        episode: Episode,
        repository: SystemHealthRepository,
    ) -> dict:
        checked_at = datetime.now(UTC).isoformat()
        model_endpoints = {endpoint.id: endpoint for endpoint in repository.list_model_endpoints()}
        voicebox_endpoints = {
            endpoint.id: endpoint for endpoint in repository.list_voicebox_endpoints()
        }
        voice_profiles = {profile.id: profile for profile in repository.list_voice_profiles()}
        comfyui_endpoints = {
            endpoint.id: endpoint for endpoint in repository.list_comfyui_endpoints()
        }
        comfyui_workflows = {
            workflow.id: workflow for workflow in repository.list_comfyui_workflows()
        }
        visual_profiles = {profile.id: profile for profile in repository.list_visual_profiles()}

        participants = [participant for participant in episode.participants if participant.enabled]
        participant_ids = {participant.id for participant in participants}
        assigned_ids = {
            assignment.participant_profile_id for assignment in episode.definition.participants
        }
        selected = [
            participant
            for participant in participants
            if participant.id in assigned_ids or not assigned_ids
        ]
        moderator_count = sum(
            1
            for assignment in episode.definition.participants
            if assignment.role == "moderator"
            and assignment.participant_profile_id in participant_ids
        )

        discussion = self._episode_pilot_discussion_readiness(
            selected,
            model_endpoints,
            moderator_count,
        )
        speech = self._episode_pilot_speech_readiness(
            selected,
            voice_profiles,
            voicebox_endpoints,
        )
        visuals = self._episode_pilot_visual_readiness(
            episode,
            selected,
            visual_profiles,
            comfyui_workflows,
            comfyui_endpoints,
        )
        rendering = self._episode_pilot_render_readiness()
        stages = [discussion, speech, visuals, rendering]
        pilot_modes = self._episode_pilot_modes(stages)
        production_target = getattr(
            episode.definition.workflow,
            "production_target",
            "native_visual",
        )
        selected_pilot_mode = next(
            (mode for mode in pilot_modes if mode["mode"] == production_target),
            None,
        )
        all_stage_blockers = [
            blocker
            for stage in stages
            for blocker in stage.get("blockers", [])
            if isinstance(blocker, str) and blocker
        ]
        all_stage_warnings = [
            warning
            for stage in stages
            for warning in stage.get("warnings", [])
            if isinstance(warning, str) and warning
        ]
        target_blockers = (
            list(selected_pilot_mode["blockers"])
            if selected_pilot_mode
            else ["configured production target has no readiness mode"]
        )
        target_warnings = list(selected_pilot_mode["warnings"]) if selected_pilot_mode else []
        target_status = selected_pilot_mode["status"] if selected_pilot_mode else "fail"
        fail_count = sum(1 for stage in stages if stage["status"] == "fail")
        warning_count = sum(1 for stage in stages if stage["status"] == "warning")
        pass_count = sum(1 for stage in stages if stage["status"] == "pass")
        return {
            "schema_version": "episode_pilot_readiness.v1",
            "episode_id": str(episode.id),
            "checked_at": checked_at,
            "status": target_status,
            "summary": {
                "stage_count": len(stages),
                "pass_count": pass_count,
                "warning_count": warning_count,
                "fail_count": fail_count,
                "blocker_count": len(target_blockers),
                "warning_item_count": len(target_warnings),
                "all_stage_blocker_count": len(all_stage_blockers),
                "all_stage_warning_item_count": len(all_stage_warnings),
                "selected_participant_count": len(selected),
            },
            "production_target": production_target,
            "target_status": target_status,
            "selected_pilot_mode": selected_pilot_mode,
            "pilot_modes": pilot_modes,
            "stages": stages,
            "blockers": target_blockers,
            "warnings": target_warnings,
            "all_stage_blockers": all_stage_blockers,
            "all_stage_warnings": all_stage_warnings,
        }

    def _episode_pilot_modes(self, stages: list[dict]) -> list[dict]:
        stage_by_category = {
            str(stage.get("category")): stage
            for stage in stages
            if isinstance(stage.get("category"), str)
        }
        return [
            self._episode_pilot_mode(
                mode="audio_first",
                label="Audio-first talkshow pilot",
                required_categories=["discussion", "speech", "rendering"],
                stage_by_category=stage_by_category,
                optional_warning_categories=["visuals"],
            ),
            self._episode_pilot_mode(
                mode="native_visual",
                label="Native animated video pilot",
                required_categories=["discussion", "speech", "visuals", "rendering"],
                stage_by_category=stage_by_category,
            ),
        ]

    def _episode_pilot_mode(
        self,
        *,
        mode: str,
        label: str,
        required_categories: list[str],
        stage_by_category: dict[str, dict],
        optional_warning_categories: list[str] | None = None,
    ) -> dict:
        required_stages = [
            stage
            for category in required_categories
            if (stage := stage_by_category.get(category)) is not None
        ]
        missing_categories = [
            category for category in required_categories if category not in stage_by_category
        ]
        blockers = [
            blocker
            for stage in required_stages
            for blocker in stage.get("blockers", [])
            if isinstance(blocker, str) and blocker
        ]
        warnings = [
            warning
            for stage in required_stages
            for warning in stage.get("warnings", [])
            if isinstance(warning, str) and warning
        ]
        for category in missing_categories:
            blockers.append(f"{category} readiness evidence is missing")

        for category in optional_warning_categories or []:
            optional_stage = stage_by_category.get(category)
            if not optional_stage or optional_stage.get("status") == "pass":
                continue
            warnings.append(f"{optional_stage.get('label', category)} is not ready for this mode")

        required_statuses = {str(stage.get("status")) for stage in required_stages}
        status = (
            "fail"
            if blockers or missing_categories or "fail" in required_statuses
            else "warning"
            if warnings or "warning" in required_statuses
            else "pass"
        )
        return {
            "mode": mode,
            "label": label,
            "status": status,
            "required_stage_categories": required_categories,
            "blockers": blockers,
            "warnings": warnings,
        }

    def _episode_pilot_discussion_readiness(
        self,
        participants: list[ParticipantProfile],
        model_endpoints: dict[str, ModelEndpoint],
        moderator_count: int,
    ) -> dict:
        missing_model = [
            participant.id
            for participant in participants
            if not participant.model_endpoint_id or not participant.model_id
        ]
        missing_endpoint = [
            participant.id
            for participant in participants
            if participant.model_endpoint_id not in model_endpoints
        ]
        disabled_endpoint = [
            participant.id
            for participant in participants
            if (endpoint := model_endpoints.get(participant.model_endpoint_id)) is not None
            and not endpoint.enabled
        ]
        unhealthy_endpoint = [
            participant.id
            for participant in participants
            if (endpoint := model_endpoints.get(participant.model_endpoint_id)) is not None
            and endpoint.health_status in {"unhealthy", "failed"}
        ]
        unknown_endpoint = [
            participant.id
            for participant in participants
            if (endpoint := model_endpoints.get(participant.model_endpoint_id)) is not None
            and endpoint.health_status == "unknown"
        ]
        remote_participants = [
            participant.id
            for participant in participants
            if (endpoint := model_endpoints.get(participant.model_endpoint_id)) is not None
            and str(endpoint.provider_type) not in {"mock", "ProviderType.mock"}
        ]
        blockers: list[str] = []
        warnings: list[str] = []
        if len(participants) < 4:
            blockers.append("pilot needs one moderator and at least three active participants")
        if moderator_count != 1:
            blockers.append("pilot needs exactly one selected moderator")
        if missing_model:
            blockers.append("one or more selected participants are missing model assignments")
        if missing_endpoint:
            blockers.append("one or more selected participants reference missing model endpoints")
        if disabled_endpoint:
            blockers.append("one or more selected participants use disabled model endpoints")
        if unhealthy_endpoint:
            blockers.append("one or more selected participants use unhealthy model endpoints")
        if len(remote_participants) != len(participants):
            blockers.append(
                "all selected participants need non-mock model endpoints for a real pilot"
            )
        if unknown_endpoint:
            warnings.append("one or more selected model endpoint health checks are unknown")
        readiness_checks = {
            "selected_participant_count_sufficient": len(participants) >= 4,
            "exactly_one_moderator_selected": moderator_count == 1,
            "selected_participants_have_model_assignments": len(missing_model) == 0,
            "selected_model_endpoints_exist": len(missing_endpoint) == 0,
            "selected_model_endpoints_enabled": len(disabled_endpoint) == 0,
            "selected_model_endpoints_not_unhealthy": len(unhealthy_endpoint) == 0,
            "selected_model_endpoints_remote": len(remote_participants) == len(participants),
            "selected_model_endpoint_health_known": len(unknown_endpoint) == 0,
        }
        return self._episode_pilot_stage(
            category="discussion",
            label="Real discussion",
            readiness_checks=readiness_checks,
            blockers=blockers,
            warnings=warnings,
            details={
                "participant_count": len(participants),
                "remote_model_participant_count": len(remote_participants),
                "missing_model_participant_ids": missing_model[:10],
                "missing_endpoint_participant_ids": missing_endpoint[:10],
                "disabled_endpoint_participant_ids": disabled_endpoint[:10],
                "unhealthy_endpoint_participant_ids": unhealthy_endpoint[:10],
                "unknown_endpoint_participant_ids": unknown_endpoint[:10],
            },
        )

    def _episode_pilot_speech_readiness(
        self,
        participants: list[ParticipantProfile],
        voice_profiles: dict[str, VoiceProfile],
        voicebox_endpoints: dict[str, VoiceboxEndpoint],
    ) -> dict:
        missing_voice = [
            participant.id for participant in participants if not participant.voice_profile_id
        ]
        missing_profile = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and participant.voice_profile_id not in voice_profiles
        ]
        disabled_profile = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and (profile := voice_profiles.get(participant.voice_profile_id)) is not None
            and not profile.enabled
        ]
        missing_endpoint = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and (profile := voice_profiles.get(participant.voice_profile_id)) is not None
            and profile.voicebox_endpoint_id not in voicebox_endpoints
        ]
        disabled_endpoint = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and (profile := voice_profiles.get(participant.voice_profile_id)) is not None
            and (endpoint := voicebox_endpoints.get(profile.voicebox_endpoint_id)) is not None
            and not endpoint.enabled
        ]
        unhealthy_endpoint = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and (profile := voice_profiles.get(participant.voice_profile_id)) is not None
            and (endpoint := voicebox_endpoints.get(profile.voicebox_endpoint_id)) is not None
            and endpoint.health_status in {"unhealthy", "failed"}
        ]
        unknown_endpoint = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and (profile := voice_profiles.get(participant.voice_profile_id)) is not None
            and (endpoint := voicebox_endpoints.get(profile.voicebox_endpoint_id)) is not None
            and endpoint.health_status == "unknown"
        ]
        remote_voice = [
            participant.id
            for participant in participants
            if participant.voice_profile_id
            and (profile := voice_profiles.get(participant.voice_profile_id)) is not None
            and (endpoint := voicebox_endpoints.get(profile.voicebox_endpoint_id)) is not None
            and endpoint.adapter_type != "mock"
        ]
        blockers: list[str] = []
        warnings: list[str] = []
        if missing_voice:
            blockers.append("one or more selected participants are missing voice profiles")
        if missing_profile:
            blockers.append("one or more selected participants reference missing voice profiles")
        if disabled_profile:
            blockers.append("one or more selected participants use disabled voice profiles")
        if missing_endpoint:
            blockers.append("one or more selected voices reference missing Voicebox endpoints")
        if disabled_endpoint:
            blockers.append("one or more selected voices use disabled Voicebox endpoints")
        if unhealthy_endpoint:
            blockers.append("one or more selected voices use unhealthy Voicebox endpoints")
        if len(remote_voice) != len(participants):
            blockers.append(
                "all selected participants need non-mock Voicebox voices for a real pilot"
            )
        if unknown_endpoint:
            warnings.append("one or more selected Voicebox endpoint health checks are unknown")
        readiness_checks = {
            "selected_participants_have_voice_profiles": len(missing_voice) == 0,
            "selected_voice_profiles_exist": len(missing_profile) == 0,
            "selected_voice_profiles_enabled": len(disabled_profile) == 0,
            "selected_voicebox_endpoints_exist": len(missing_endpoint) == 0,
            "selected_voicebox_endpoints_enabled": len(disabled_endpoint) == 0,
            "selected_voicebox_endpoints_not_unhealthy": len(unhealthy_endpoint) == 0,
            "selected_voicebox_endpoints_remote": len(remote_voice) == len(participants),
            "selected_voicebox_endpoint_health_known": len(unknown_endpoint) == 0,
        }
        return self._episode_pilot_stage(
            category="speech",
            label="Character speech",
            readiness_checks=readiness_checks,
            blockers=blockers,
            warnings=warnings,
            details={
                "participant_count": len(participants),
                "remote_voice_participant_count": len(remote_voice),
                "missing_voice_participant_ids": missing_voice[:10],
                "missing_profile_participant_ids": missing_profile[:10],
                "disabled_profile_participant_ids": disabled_profile[:10],
                "missing_endpoint_participant_ids": missing_endpoint[:10],
                "disabled_endpoint_participant_ids": disabled_endpoint[:10],
                "unhealthy_endpoint_participant_ids": unhealthy_endpoint[:10],
                "unknown_endpoint_participant_ids": unknown_endpoint[:10],
            },
        )

    def _episode_pilot_visual_readiness(
        self,
        episode: Episode,
        participants: list[ParticipantProfile],
        visual_profiles: dict[str, VisualProfile],
        comfyui_workflows: dict[str, ComfyUiWorkflow],
        comfyui_endpoints: dict[str, ComfyUiEndpoint],
    ) -> dict:
        seated_panel_required_presets = self._seated_panel_required_media_presets(episode)
        seated_panel_workflow_issues = self._seated_panel_workflow_endpoint_issues(
            episode,
            comfyui_workflows,
            comfyui_endpoints,
        )
        missing_visual = [
            participant.id for participant in participants if not participant.visual_profile_id
        ]
        missing_profile = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and participant.visual_profile_id not in visual_profiles
        ]
        disabled_profile = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and not profile.enabled
        ]
        missing_portrait = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and not self._visual_profile_has_reference(profile, "portrait")
        ]
        missing_full_body = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and not self._visual_profile_has_reference(profile, "full_body")
        ]
        missing_workflow = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and profile.primary_workflow_id not in comfyui_workflows
        ]
        disabled_workflow = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and (workflow := comfyui_workflows.get(profile.primary_workflow_id)) is not None
            and not workflow.enabled
        ]
        missing_endpoint = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and (workflow := comfyui_workflows.get(profile.primary_workflow_id)) is not None
            and workflow.comfyui_endpoint_id not in comfyui_endpoints
        ]
        disabled_endpoint = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and (workflow := comfyui_workflows.get(profile.primary_workflow_id)) is not None
            and (endpoint := comfyui_endpoints.get(workflow.comfyui_endpoint_id)) is not None
            and not endpoint.enabled
        ]
        unhealthy_endpoint = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and (workflow := comfyui_workflows.get(profile.primary_workflow_id)) is not None
            and (endpoint := comfyui_endpoints.get(workflow.comfyui_endpoint_id)) is not None
            and endpoint.health_status in {"unhealthy", "failed"}
        ]
        unknown_endpoint = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and (workflow := comfyui_workflows.get(profile.primary_workflow_id)) is not None
            and (endpoint := comfyui_endpoints.get(workflow.comfyui_endpoint_id)) is not None
            and endpoint.health_status == "unknown"
        ]
        prompt_admission_blocked_endpoints = self._visual_prompt_admission_blocked_endpoints(
            participants,
            visual_profiles,
            comfyui_workflows,
            comfyui_endpoints,
        )
        managed_media_missing_preset_endpoints = (
            self._visual_managed_media_missing_preset_endpoints(
                participants,
                visual_profiles,
                comfyui_workflows,
                comfyui_endpoints,
                additional_required_presets=seated_panel_required_presets,
            )
        )
        managed_media_required_endpoints = self._visual_managed_media_required_endpoints(
            participants,
            visual_profiles,
            comfyui_workflows,
            comfyui_endpoints,
            additional_required_presets=seated_panel_required_presets,
        )
        seated_panel_missing_preset_endpoints = [
            entry
            for entry in managed_media_missing_preset_endpoints
            if "studio-panel-shot" in entry.get("missing_presets", [])
        ]
        remote_visual = [
            participant.id
            for participant in participants
            if participant.visual_profile_id
            and (profile := visual_profiles.get(participant.visual_profile_id)) is not None
            and (workflow := comfyui_workflows.get(profile.primary_workflow_id)) is not None
            and (endpoint := comfyui_endpoints.get(workflow.comfyui_endpoint_id)) is not None
            and endpoint.adapter_type != "mock"
        ]
        blockers: list[str] = []
        warnings: list[str] = []
        if missing_visual:
            blockers.append("one or more selected participants are missing visual profiles")
        if missing_profile:
            blockers.append("one or more selected participants reference missing visual profiles")
        if disabled_profile:
            blockers.append("one or more selected participants use disabled visual profiles")
        if missing_portrait:
            blockers.append("one or more selected characters are missing portrait references")
        if missing_full_body:
            blockers.append("one or more selected characters are missing full-body references")
        if missing_workflow:
            blockers.append("one or more selected visuals reference missing ComfyUI workflows")
        if disabled_workflow:
            blockers.append("one or more selected visuals use disabled ComfyUI workflows")
        if missing_endpoint:
            blockers.append(
                "one or more selected visual workflows reference missing ComfyUI endpoints"
            )
        if disabled_endpoint:
            blockers.append("one or more selected visual workflows use disabled ComfyUI endpoints")
        if unhealthy_endpoint:
            blockers.append("one or more selected visual workflows use unhealthy ComfyUI endpoints")
        if prompt_admission_blocked_endpoints:
            blockers.append("one or more selected native ComfyUI endpoints block prompt admission")
        if managed_media_missing_preset_endpoints:
            if seated_panel_missing_preset_endpoints:
                blockers.append(
                    "B1 studio-panel-shot is unavailable for seated panel production"
                )
            else:
                blockers.append("one or more selected B1 managed media presets are unavailable")
        if seated_panel_workflow_issues:
            blockers.append(
                "one or more seated panel workflows cannot submit to an enabled "
                "remote media endpoint"
            )
        if len(remote_visual) != len(participants):
            warnings.append("visual pilot will use mock ComfyUI for one or more characters")
        if unknown_endpoint:
            warnings.append("one or more selected ComfyUI endpoint health checks are unknown")
        readiness_checks = {
            "selected_participants_have_visual_profiles": len(missing_visual) == 0,
            "selected_visual_profiles_exist": len(missing_profile) == 0,
            "selected_visual_profiles_enabled": len(disabled_profile) == 0,
            "selected_visual_profiles_have_portraits": len(missing_portrait) == 0,
            "selected_visual_profiles_have_full_body_references": len(missing_full_body) == 0,
            "selected_visual_workflows_exist": len(missing_workflow) == 0,
            "selected_visual_workflows_enabled": len(disabled_workflow) == 0,
            "selected_comfyui_endpoints_exist": len(missing_endpoint) == 0,
            "selected_comfyui_endpoints_enabled": len(disabled_endpoint) == 0,
            "selected_comfyui_endpoints_not_unhealthy": len(unhealthy_endpoint) == 0,
            "selected_comfyui_endpoint_health_known": len(unknown_endpoint) == 0,
            "selected_native_comfyui_prompt_admission_ready": (
                len(prompt_admission_blocked_endpoints) == 0
            ),
            "selected_b1_managed_media_presets_available": (
                len(managed_media_missing_preset_endpoints) == 0
            ),
            "selected_seated_panel_media_available": (
                len(seated_panel_missing_preset_endpoints) == 0
            ),
            "selected_seated_panel_workflows_configured": (
                len(seated_panel_workflow_issues) == 0
            ),
        }
        return self._episode_pilot_stage(
            category="visuals",
            label="Character animation",
            readiness_checks=readiness_checks,
            blockers=blockers,
            warnings=warnings,
            details={
                "participant_count": len(participants),
                "remote_visual_participant_count": len(remote_visual),
                "missing_visual_participant_ids": missing_visual[:10],
                "missing_profile_participant_ids": missing_profile[:10],
                "disabled_profile_participant_ids": disabled_profile[:10],
                "missing_portrait_participant_ids": missing_portrait[:10],
                "missing_full_body_participant_ids": missing_full_body[:10],
                "missing_workflow_participant_ids": missing_workflow[:10],
                "disabled_workflow_participant_ids": disabled_workflow[:10],
                "missing_endpoint_participant_ids": missing_endpoint[:10],
                "disabled_endpoint_participant_ids": disabled_endpoint[:10],
                "unhealthy_endpoint_participant_ids": unhealthy_endpoint[:10],
                "unknown_endpoint_participant_ids": unknown_endpoint[:10],
                "prompt_admission_blocked_endpoints": (
                    prompt_admission_blocked_endpoints[:10]
                ),
                "managed_media_missing_preset_endpoints": (
                    managed_media_missing_preset_endpoints[:10]
                ),
                "managed_media_required_endpoints": managed_media_required_endpoints[:10],
                "seated_panel_required_presets": list(seated_panel_required_presets),
                "seated_panel_missing_preset_endpoints": (
                    seated_panel_missing_preset_endpoints[:10]
                ),
                "seated_panel_workflow_issues": seated_panel_workflow_issues,
            },
        )

    @staticmethod
    def _seated_panel_required_media_presets(episode: Episode) -> tuple[str, ...]:
        directing = episode.definition.media.directing
        if directing.mode != "studio_directed" or directing.studio_layout != "seated_panel":
            return ()
        return ("studio-seated-character-p40", "studio-panel-shot")

    @staticmethod
    def _seated_panel_workflow_endpoint_issues(
        episode: Episode,
        comfyui_workflows: dict[str, ComfyUiWorkflow],
        comfyui_endpoints: dict[str, ComfyUiEndpoint],
    ) -> list[dict]:
        """Verify the special panel workflows, which are not profile-owned workflows."""
        if not SystemHealthService._seated_panel_required_media_presets(episode):
            return []
        required_types = (
            "studio_seated_character",
            "studio_panel_shot",
            "seated_panel_lipsync",
        )
        issues: list[dict] = []
        for workflow_type in required_types:
            workflow = next(
                (
                    candidate
                    for candidate in comfyui_workflows.values()
                    if candidate.workflow_type == workflow_type
                ),
                None,
            )
            if workflow is None:
                issues.append({"workflow_type": workflow_type, "issue": "workflow_missing"})
                continue
            endpoint = comfyui_endpoints.get(workflow.comfyui_endpoint_id)
            if not workflow.enabled:
                issues.append(
                    {
                        "workflow_id": workflow.id,
                        "workflow_type": workflow_type,
                        "issue": "workflow_disabled",
                    }
                )
                continue
            if not workflow.default_parameters.get("managed_b1_media_api"):
                issues.append(
                    {
                        "workflow_id": workflow.id,
                        "workflow_type": workflow_type,
                        "issue": "managed_media_api_not_configured",
                    }
                )
                continue
            if endpoint is None:
                issues.append(
                    {
                        "workflow_id": workflow.id,
                        "workflow_type": workflow_type,
                        "endpoint_id": workflow.comfyui_endpoint_id,
                        "issue": "endpoint_missing",
                    }
                )
                continue
            if not endpoint.enabled:
                issues.append(
                    {
                        "workflow_id": workflow.id,
                        "workflow_type": workflow_type,
                        "endpoint_id": endpoint.id,
                        "issue": "endpoint_disabled",
                    }
                )
                continue
            if endpoint.adapter_type == "mock":
                issues.append(
                    {
                        "workflow_id": workflow.id,
                        "workflow_type": workflow_type,
                        "endpoint_id": endpoint.id,
                        "issue": "remote_managed_media_endpoint_required",
                    }
                )
                continue
            if endpoint.health_status in {"unhealthy", "failed", "unknown"}:
                issues.append(
                    {
                        "workflow_id": workflow.id,
                        "workflow_type": workflow_type,
                        "endpoint_id": endpoint.id,
                        "issue": f"endpoint_{endpoint.health_status}",
                    }
                )
        return issues

    def _visual_prompt_admission_blocked_endpoints(
        self,
        participants: list[ParticipantProfile],
        visual_profiles: dict[str, VisualProfile],
        comfyui_workflows: dict[str, ComfyUiWorkflow],
        comfyui_endpoints: dict[str, ComfyUiEndpoint],
    ) -> list[dict]:
        blocked_by_endpoint: dict[str, dict] = {}
        for participant in participants:
            if not participant.visual_profile_id:
                continue
            profile = visual_profiles.get(participant.visual_profile_id)
            if profile is None:
                continue
            workflow = comfyui_workflows.get(profile.primary_workflow_id)
            if workflow is None:
                continue
            endpoint = comfyui_endpoints.get(workflow.comfyui_endpoint_id)
            if endpoint is None:
                continue
            admission = self._prompt_admission_readiness_entry(endpoint)
            if not admission or admission.get("ready") is True:
                continue
            entry = blocked_by_endpoint.setdefault(
                endpoint.id,
                {
                    "endpoint": self._media_endpoint_readiness_entry(endpoint),
                    "participant_ids": [],
                    "workflow_ids": [],
                    "visual_profile_ids": [],
                },
            )
            entry["participant_ids"].append(participant.id)
            if workflow.id not in entry["workflow_ids"]:
                entry["workflow_ids"].append(workflow.id)
            if profile.id not in entry["visual_profile_ids"]:
                entry["visual_profile_ids"].append(profile.id)
        return list(blocked_by_endpoint.values())

    def _visual_managed_media_missing_preset_endpoints(
        self,
        participants: list[ParticipantProfile],
        visual_profiles: dict[str, VisualProfile],
        comfyui_workflows: dict[str, ComfyUiWorkflow],
        comfyui_endpoints: dict[str, ComfyUiEndpoint],
        additional_required_presets: tuple[str, ...] = (),
    ) -> list[dict]:
        blocked_by_endpoint: dict[str, dict] = {}
        for participant in participants:
            if not participant.visual_profile_id:
                continue
            profile = visual_profiles.get(participant.visual_profile_id)
            if profile is None:
                continue
            workflow = comfyui_workflows.get(profile.primary_workflow_id)
            if workflow is None or not workflow.default_parameters.get("managed_b1_media_api"):
                continue
            preset = str(workflow.default_parameters.get("b1_media_preset") or "").strip()
            if not preset:
                continue
            endpoint = comfyui_endpoints.get(workflow.comfyui_endpoint_id)
            if endpoint is None:
                continue
            if endpoint.adapter_type == "mock":
                continue
            capabilities = endpoint.capabilities if isinstance(endpoint.capabilities, dict) else {}
            available_presets = {
                str(item)
                for item in capabilities.get("managed_media_available_presets", [])
                if str(item).strip()
            }
            # The fixed managed-media preset list predates operation-specific models
            # such as `studio-panel-shot`. Keep it for the existing readiness surface,
            # but use B1's complete enabled model catalog for episode-specific checks.
            available_presets.update(
                str(item)
                for item in capabilities.get("managed_media_available_model_ids", [])
                if str(item).strip()
            )
            missing_presets = {
                str(item)
                for item in capabilities.get("managed_media_missing_presets", [])
                if str(item).strip()
            }
            catalog_known = (
                "managed_media_available_presets" in capabilities
                or "managed_media_missing_presets" in capabilities
                or "managed_media_available_model_ids" in capabilities
            )
            for required_preset in dict.fromkeys((preset, *additional_required_presets)):
                if (
                    catalog_known
                    and required_preset in available_presets
                    and required_preset not in missing_presets
                ):
                    continue
                entry = blocked_by_endpoint.setdefault(
                    endpoint.id,
                    {
                        "endpoint": self._media_endpoint_readiness_entry(endpoint),
                        "participant_ids": [],
                        "workflow_ids": [],
                        "visual_profile_ids": [],
                        "required_presets": [],
                        "missing_presets": [],
                    },
                )
                entry["participant_ids"].append(participant.id)
                if workflow.id not in entry["workflow_ids"]:
                    entry["workflow_ids"].append(workflow.id)
                if profile.id not in entry["visual_profile_ids"]:
                    entry["visual_profile_ids"].append(profile.id)
                if required_preset not in entry["required_presets"]:
                    entry["required_presets"].append(required_preset)
                if (
                    required_preset not in available_presets
                    and required_preset not in entry["missing_presets"]
                ):
                    entry["missing_presets"].append(required_preset)
                for missing in sorted(missing_presets):
                    if missing not in entry["missing_presets"]:
                        entry["missing_presets"].append(missing)
        return list(blocked_by_endpoint.values())

    def _visual_managed_media_required_endpoints(
        self,
        participants: list[ParticipantProfile],
        visual_profiles: dict[str, VisualProfile],
        comfyui_workflows: dict[str, ComfyUiWorkflow],
        comfyui_endpoints: dict[str, ComfyUiEndpoint],
        additional_required_presets: tuple[str, ...] = (),
    ) -> list[dict]:
        by_endpoint: dict[str, dict] = {}
        for participant in participants:
            if not participant.visual_profile_id:
                continue
            profile = visual_profiles.get(participant.visual_profile_id)
            if profile is None:
                continue
            workflow = comfyui_workflows.get(profile.primary_workflow_id)
            if workflow is None or not workflow.default_parameters.get("managed_b1_media_api"):
                continue
            preset = str(workflow.default_parameters.get("b1_media_preset") or "").strip()
            if not preset:
                continue
            endpoint = comfyui_endpoints.get(workflow.comfyui_endpoint_id)
            if endpoint is None:
                continue
            if endpoint.adapter_type == "mock":
                continue
            capabilities = endpoint.capabilities if isinstance(endpoint.capabilities, dict) else {}
            available_presets = {
                str(item)
                for item in capabilities.get("managed_media_available_presets", [])
                if str(item).strip()
            }
            available_presets.update(
                str(item)
                for item in capabilities.get("managed_media_available_model_ids", [])
                if str(item).strip()
            )
            missing_presets = [
                str(item)
                for item in capabilities.get("managed_media_missing_presets", [])
                if str(item).strip()
            ]
            entry = by_endpoint.setdefault(
                endpoint.id,
                {
                    "endpoint": self._media_endpoint_readiness_entry(endpoint),
                    "participant_ids": [],
                    "workflow_ids": [],
                    "visual_profile_ids": [],
                    "required_presets": [],
                    "available_presets": sorted(available_presets),
                    "missing_presets": sorted(set(missing_presets)),
                    "catalog_ready": capabilities.get("managed_media_catalog_ready") is True,
                    "catalog_status_code": capabilities.get(
                        "managed_media_catalog_status_code"
                    ),
                    "model_count": capabilities.get("managed_media_model_count"),
                },
            )
            entry["participant_ids"].append(participant.id)
            if workflow.id not in entry["workflow_ids"]:
                entry["workflow_ids"].append(workflow.id)
            if profile.id not in entry["visual_profile_ids"]:
                entry["visual_profile_ids"].append(profile.id)
            for required_preset in dict.fromkeys((preset, *additional_required_presets)):
                if required_preset not in entry["required_presets"]:
                    entry["required_presets"].append(required_preset)
        for entry in by_endpoint.values():
            entry["required_presets"] = sorted(entry["required_presets"])
        return list(by_endpoint.values())

    def _episode_pilot_render_readiness(self) -> dict:
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        blockers: list[str] = []
        if not ffmpeg_path:
            blockers.append("ffmpeg must be available before preview or final video rendering")
        if not ffprobe_path:
            blockers.append("ffprobe must be available before render QC")
        readiness_checks = {
            "ffmpeg_available": bool(ffmpeg_path),
            "ffprobe_available": bool(ffprobe_path),
        }
        return self._episode_pilot_stage(
            category="rendering",
            label="Preview and final render",
            readiness_checks=readiness_checks,
            blockers=blockers,
            warnings=[],
            details={
                "ffmpeg_path": ffmpeg_path,
                "ffprobe_path": ffprobe_path,
            },
        )

    def _episode_pilot_stage(
        self,
        *,
        category: str,
        label: str,
        readiness_checks: dict[str, bool],
        blockers: list[str],
        warnings: list[str],
        details: dict,
    ) -> dict:
        return {
            "category": category,
            "label": label,
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "details": details
            | {
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _visual_profile_has_reference(self, profile: VisualProfile, reference_type: str) -> bool:
        if reference_type == "portrait" and profile.reference_image_uri:
            return True
        return any(image.reference_type == reference_type for image in profile.reference_images)

    def credential_provisioning_plan(
        self,
        repository: SystemHealthRepository,
        include_disabled: bool = True,
    ) -> dict:
        try:
            model_endpoints = repository.list_model_endpoints()
            voicebox_endpoints = repository.list_voicebox_endpoints()
            comfyui_endpoints = repository.list_comfyui_endpoints()
            publisher_targets = repository.list_publisher_targets()
        except Exception as exc:
            return {
                "schema_version": "credential_provisioning_plan.v1",
                "status": "fail",
                "checked_at": datetime.now(UTC).isoformat(),
                "include_disabled": include_disabled,
                "summary": {
                    "reference_count": 0,
                    "resolved_count": 0,
                    "unavailable_count": 0,
                    "unsupported_count": 0,
                },
                "references": [],
                "env_vars": [],
                "docker_secrets": [],
                "files": [],
                "blockers": [f"repository query failed: {type(exc).__name__}"],
            }

        entries = self._credential_provisioning_entries(
            model_endpoints=model_endpoints,
            voicebox_endpoints=voicebox_endpoints,
            comfyui_endpoints=comfyui_endpoints,
            publisher_targets=publisher_targets,
            include_disabled=include_disabled,
        )
        checked = [self._credential_provisioning_entry_status(entry) for entry in entries]
        env_vars = sorted(
            {item["target"] for item in checked if item["scheme"] == "env" and item.get("target")}
        )
        docker_secrets = sorted(
            {
                item["target"]
                for item in checked
                if item["scheme"] == "docker-secret" and item.get("target")
            }
        )
        files = sorted(
            {item["target"] for item in checked if item["scheme"] == "file" and item.get("target")}
        )
        unsupported_count = sum(1 for item in checked if item["scheme"] == "unsupported")
        invalid_count = sum(1 for item in checked if item["scheme"] == "invalid")
        unavailable_count = sum(1 for item in checked if item["status"] == "unavailable")
        blockers = [
            f"{item['reference']} for {item['owner_type']}:{item['owner_id']} is unavailable"
            for item in checked
            if item["status"] == "unavailable"
        ]
        if unsupported_count:
            blockers.append("one or more credential references use an unsupported scheme")
        if invalid_count:
            blockers.append("one or more credential references have invalid syntax")
        return {
            "schema_version": "credential_provisioning_plan.v1",
            "status": "fail" if blockers else "pass",
            "checked_at": datetime.now(UTC).isoformat(),
            "include_disabled": include_disabled,
            "summary": {
                "reference_count": len(checked),
                "resolved_count": sum(1 for item in checked if item["status"] == "resolved"),
                "unavailable_count": unavailable_count,
                "unsupported_count": unsupported_count,
                "invalid_count": invalid_count,
                "env_var_count": len(env_vars),
                "docker_secret_count": len(docker_secrets),
                "file_count": len(files),
            },
            "references": checked,
            "env_vars": env_vars,
            "docker_secrets": docker_secrets,
            "files": files,
            "compose_environment_examples": [f"{name}=<provisioned-secret>" for name in env_vars],
            "docker_secret_examples": [f"{name}: external: true" for name in docker_secrets],
            "blockers": blockers,
        }

    def _credential_provisioning_entries(
        self,
        model_endpoints: list[ModelEndpoint],
        voicebox_endpoints: list[VoiceboxEndpoint],
        comfyui_endpoints: list[ComfyUiEndpoint],
        publisher_targets: list[PublisherTarget],
        include_disabled: bool,
    ) -> list[dict]:
        entries: list[dict] = []
        self._append_settings_credential_references(entries)
        for endpoint in model_endpoints:
            if include_disabled or endpoint.enabled:
                self._append_credential_reference(
                    entries,
                    owner_type="model_endpoint",
                    owner_id=endpoint.id,
                    field="credential_reference",
                    reference=endpoint.credential_reference,
                )
        for endpoint in voicebox_endpoints:
            if include_disabled or endpoint.enabled:
                self._append_credential_reference(
                    entries,
                    owner_type="voicebox_endpoint",
                    owner_id=endpoint.id,
                    field="credential_reference",
                    reference=endpoint.credential_reference,
                )
        for endpoint in comfyui_endpoints:
            if include_disabled or endpoint.enabled:
                self._append_credential_reference(
                    entries,
                    owner_type="comfyui_endpoint",
                    owner_id=endpoint.id,
                    field="credential_reference",
                    reference=endpoint.credential_reference,
                )
        for target in publisher_targets:
            if not include_disabled and not target.enabled:
                continue
            self._append_credential_reference(
                entries,
                owner_type="publisher_target",
                owner_id=target.id,
                field="credential_reference",
                reference=target.credential_reference,
            )
            for field in (
                "oauth_refresh_token_reference",
                "oauth_client_id_reference",
                "oauth_client_secret_reference",
            ):
                value = target.capabilities.get(field)
                self._append_credential_reference(
                    entries,
                    owner_type="publisher_target",
                    owner_id=target.id,
                    field=f"capabilities.{field}",
                    reference=value if isinstance(value, str) else None,
                )
        return entries

    def _credential_provisioning_entry_status(self, entry: dict) -> dict:
        target = public_credential_target(str(entry["reference"]))
        raw_reference = str(entry.get("_credential_reference") or entry["reference"])
        public_entry = {
            key: value for key, value in entry.items() if key != "_credential_reference"
        }
        result = public_entry | {"target": target}
        if entry["scheme"] == "unsupported":
            return result | {
                "status": "unavailable",
                "reason": "unsupported credential reference scheme",
            }
        try:
            self.secret_resolver.resolve(raw_reference)
        except RuntimeError as exc:
            return result | {
                "status": "unavailable",
                "error": type(exc).__name__,
                "reason": str(exc),
            }
        return result | {"status": "resolved"}

    def _credential_provisioning_check(
        self,
        model_endpoints: list[ModelEndpoint],
        voicebox_endpoints: list[VoiceboxEndpoint],
        comfyui_endpoints: list[ComfyUiEndpoint],
        publisher_targets: list[PublisherTarget],
    ) -> dict:
        active_entries = self._credential_provisioning_entries(
            model_endpoints=model_endpoints,
            voicebox_endpoints=voicebox_endpoints,
            comfyui_endpoints=comfyui_endpoints,
            publisher_targets=publisher_targets,
            include_disabled=False,
        )
        all_entries = self._credential_provisioning_entries(
            model_endpoints=model_endpoints,
            voicebox_endpoints=voicebox_endpoints,
            comfyui_endpoints=comfyui_endpoints,
            publisher_targets=publisher_targets,
            include_disabled=True,
        )
        active = [self._credential_provisioning_entry_status(entry) for entry in active_entries]
        all_references = [
            self._credential_provisioning_entry_status(entry) for entry in all_entries
        ]
        active_unavailable = sum(1 for item in active if item["status"] == "unavailable")
        all_unavailable = sum(1 for item in all_references if item["status"] == "unavailable")
        inactive_unavailable = max(0, all_unavailable - active_unavailable)
        active_keys = {
            (item["owner_type"], item["owner_id"], item["field"], item["reference"])
            for item in active
        }
        missing_active = [
            self._credential_provisioning_issue_entry(item)
            for item in active
            if item["status"] == "unavailable"
        ]
        missing_inactive = [
            self._credential_provisioning_issue_entry(item)
            for item in all_references
            if item["status"] == "unavailable"
            and (
                item["owner_type"],
                item["owner_id"],
                item["field"],
                item["reference"],
            )
            not in active_keys
        ]
        env_vars = {
            item["target"]
            for item in all_references
            if item["scheme"] == "env" and item.get("target")
        }
        docker_secrets = {
            item["target"]
            for item in all_references
            if item["scheme"] == "docker-secret" and item.get("target")
        }
        files = {
            item["target"]
            for item in all_references
            if item["scheme"] == "file" and item.get("target")
        }
        reason = "all active credential references resolve"
        if active_unavailable:
            reason = "one or more active credential references are unavailable"
        elif inactive_unavailable:
            reason = (
                "active credential references resolve; disabled live targets still need "
                "credential provisioning"
            )
        readiness_checks = {
            "active_credential_references_resolve": active_unavailable == 0,
            "disabled_target_credential_references_resolve": inactive_unavailable == 0,
            "credential_reference_schemes_supported": all(
                item["scheme"] not in {"unsupported", "invalid"} for item in all_references
            ),
        }
        return {
            "name": "credential_provisioning",
            "status": "degraded" if active_unavailable else "healthy",
            "details": {
                "schema_version": "credential_provisioning_health.v1",
                "active_reference_count": len(active),
                "active_unavailable_count": active_unavailable,
                "all_reference_count": len(all_references),
                "all_unavailable_count": all_unavailable,
                "inactive_unavailable_count": inactive_unavailable,
                "env_var_count": len(env_vars),
                "docker_secret_count": len(docker_secrets),
                "file_count": len(files),
                "unsupported_count": sum(
                    1 for item in all_references if item["scheme"] == "unsupported"
                ),
                "invalid_count": sum(1 for item in all_references if item["scheme"] == "invalid"),
                "missing_active_references": missing_active[:8],
                "missing_inactive_references": missing_inactive[:8],
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
                "reason": reason,
            },
        }

    def _credential_provisioning_issue_entry(self, item: dict) -> dict:
        return {
            "owner_type": item.get("owner_type"),
            "owner_id": item.get("owner_id"),
            "field": item.get("field"),
            "reference": item.get("reference"),
            "scheme": item.get("scheme"),
            "target": item.get("target"),
            "reason": item.get("reason"),
        }

    def _credential_provisioning_readiness(
        self,
        model_endpoints: list[ModelEndpoint],
        voicebox_endpoints: list[VoiceboxEndpoint],
        comfyui_endpoints: list[ComfyUiEndpoint],
        publisher_targets: list[PublisherTarget],
    ) -> dict:
        component = self._credential_provisioning_check(
            model_endpoints=model_endpoints,
            voicebox_endpoints=voicebox_endpoints,
            comfyui_endpoints=comfyui_endpoints,
            publisher_targets=publisher_targets,
        )
        details = component["details"]
        active_unavailable = int(details.get("active_unavailable_count") or 0)
        inactive_unavailable = int(details.get("inactive_unavailable_count") or 0)
        blockers: list[str] = []
        warnings: list[str] = []
        if active_unavailable:
            blockers.append("one or more active credential references are unavailable")
        if inactive_unavailable:
            warnings.append(
                "one or more disabled live-target credential references are unavailable"
            )
        return {
            "category": "credential_provisioning",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Credential provisioning",
            "details": details
            | {
                "live_readiness_policy": (
                    "active missing credentials block live runs; missing disabled-target "
                    "credentials warn for future live cutover"
                ),
                "attention_count": active_unavailable + inactive_unavailable,
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _readiness_check_from_component(self, component: dict) -> dict:
        details = component.get("details") or {}
        status = component.get("status")
        category = str(component.get("name") or "unknown")
        mapped_status = "pass"
        if status == "unhealthy":
            mapped_status = "fail"
        elif status in {"degraded", "unknown"}:
            mapped_status = "warning"
        if status == "degraded" and category in {
            "credential_references",
            "object_storage",
            "auth_runtime",
            "temporal_runtime",
            "runtime_paths",
        }:
            mapped_status = "fail"
        if (
            status == "degraded"
            and category == "deployment_readiness"
            and details.get("production_mode") is True
        ):
            mapped_status = "fail"
        if (
            status == "degraded"
            and category == "database_migrations"
            and details.get("enforced") is True
        ):
            mapped_status = "fail"
        if (
            status == "degraded"
            and category == "redis"
            and (
                details.get("event_fanout_enabled") is True
                or details.get("worker_signal_enabled") is True
            )
        ):
            mapped_status = "fail"
        reason = str(details.get("reason") or "")
        return {
            "category": category,
            "status": mapped_status,
            "label": category.replace("_", " ").title(),
            "details": details,
            "blockers": [reason] if mapped_status == "fail" and reason else [],
            "warnings": [reason] if mapped_status == "warning" and reason else [],
        }

    def _worker_registry_check(self, worker_status: WorkerStatusSummary | None) -> dict:
        if worker_status is None:
            readiness_checks = {
                "worker_status_supplied": False,
                "active_worker_heartbeats_present": False,
                "configured_worker_roles_covered": False,
                "worker_heartbeats_fresh": False,
                "worker_heartbeats_not_failed": False,
                "worker_heartbeats_not_degraded": False,
                "worker_runtime_state_files_parse": False,
            }
            return {
                "name": "worker_registry",
                "status": "degraded",
                "details": {
                    "schema_version": "worker_registry_readiness.v1",
                    "readiness_checks": readiness_checks,
                    "failed_readiness_checks": [
                        name for name, ready in readiness_checks.items() if not ready
                    ],
                    "reason": "worker status was not supplied",
                },
            }
        counts = worker_status.counts
        active_workers = int(counts.get("active_workers") or 0)
        stale_workers = int(counts.get("stale_workers") or 0)
        failed_workers = int(counts.get("failed_workers") or 0)
        degraded_workers = int(counts.get("degraded_workers") or 0)
        malformed_heartbeats = int(counts.get("malformed_heartbeats") or 0)
        malformed_leases = int(counts.get("malformed_leases") or 0)
        active_roles = int(counts.get("active_roles") or 0)
        configured_roles = int(counts.get("configured_roles") or 0)
        status = worker_status.status
        reason = "worker registry has active heartbeat evidence"
        if failed_workers:
            reason = "one or more worker heartbeats report failed status"
        elif malformed_heartbeats or malformed_leases:
            reason = "one or more worker runtime-state files are malformed"
        elif active_workers == 0:
            reason = "no active worker heartbeats are present"
        elif stale_workers or degraded_workers:
            reason = "one or more worker heartbeats are stale or degraded"
        elif active_roles < configured_roles:
            status = "degraded"
            reason = "not all configured worker roles have active heartbeat evidence"
        role_details = self._worker_role_readiness_details(worker_status)
        readiness_checks = {
            "worker_status_supplied": True,
            "active_worker_heartbeats_present": active_workers > 0,
            "configured_worker_roles_covered": active_roles >= configured_roles,
            "worker_heartbeats_fresh": stale_workers == 0,
            "worker_heartbeats_not_failed": failed_workers == 0,
            "worker_heartbeats_not_degraded": degraded_workers == 0,
            "worker_runtime_state_files_parse": (
                malformed_heartbeats == 0 and malformed_leases == 0
            ),
        }
        failed_readiness_checks = [
            name for name, ready in readiness_checks.items() if not ready
        ]
        if not failed_readiness_checks:
            status = "healthy"
        elif failed_workers or malformed_heartbeats or malformed_leases:
            status = "unhealthy"
        else:
            status = "degraded"
        return {
            "name": "worker_registry",
            "status": status,
            "details": counts
            | {
                "schema_version": "worker_registry_readiness.v1",
                "heartbeat_ttl_seconds": worker_status.heartbeat_ttl_seconds,
                "lease_ttl_seconds": worker_status.lease_ttl_seconds,
                "runtime_state_retention_seconds": (worker_status.runtime_state_retention_seconds),
                "configured_role_names": role_details["configured_role_names"],
                "active_role_names": role_details["active_role_names"],
                "missing_role_names": role_details["missing_role_names"],
                "stale_role_names": role_details["stale_role_names"],
                "failed_role_names": role_details["failed_role_names"],
                "degraded_role_names": role_details["degraded_role_names"],
                "by_worker_status": role_details["by_worker_status"],
                "by_worker_role": role_details["by_worker_role"],
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": failed_readiness_checks,
                "reason": reason,
            },
        }

    def _worker_role_readiness_details(self, worker_status: WorkerStatusSummary) -> dict:
        configured_roles = configured_worker_roles(self.settings)
        current_role_workers = {
            role: [
                worker
                for worker in worker_status.workers
                if worker.role == role and not worker.stale
            ]
            for role in configured_roles
        }
        covered_role_names = sorted(
            {
                worker.role
                for worker in worker_status.workers
                if worker.role in configured_roles and not worker.stale
            }
        )
        stale_role_names = sorted(
            {
                role
                for role in configured_roles
                if not current_role_workers[role]
                and any(worker.role == role and worker.stale for worker in worker_status.workers)
            }
        )
        failed_role_names = sorted(
            {
                role
                for role, role_workers in current_role_workers.items()
                if role_workers
                and any(worker.status == "failed" for worker in role_workers)
                and not any(worker.status != "failed" for worker in role_workers)
            }
        )
        degraded_role_names = sorted(
            {
                role
                for role, role_workers in current_role_workers.items()
                if role_workers
                and any(worker.status == "degraded" for worker in role_workers)
                and not any(
                    worker.status not in {"failed", "degraded"}
                    for worker in role_workers
                )
            }
        )
        by_worker_status: dict[str, int] = {}
        by_worker_role: dict[str, int] = {}
        for worker in worker_status.workers:
            by_worker_status[worker.status] = by_worker_status.get(worker.status, 0) + 1
            by_worker_role[worker.role] = by_worker_role.get(worker.role, 0) + 1
        return {
            "configured_role_names": configured_roles,
            "active_role_names": covered_role_names,
            "missing_role_names": [
                role
                for role in configured_roles
                if role not in set(covered_role_names)
            ],
            "stale_role_names": stale_role_names,
            "failed_role_names": failed_role_names,
            "degraded_role_names": degraded_role_names,
            "by_worker_status": dict(sorted(by_worker_status.items())),
            "by_worker_role": dict(sorted(by_worker_role.items())),
        }

    def _worker_signal_readiness(self, summary: dict | None) -> dict:
        if summary is None:
            readiness_checks = {
                "worker_signal_summary_supplied": False,
                "worker_signal_delivery_not_failed": False,
                "worker_signals_not_blocking": False,
            }
            return {
                "category": "worker_signals",
                "status": "warning",
                "label": "Worker signals",
                "details": {
                    "schema_version": "worker_signal_readiness.v1",
                    "readiness_checks": readiness_checks,
                    "failed_readiness_checks": [
                        name for name, ready in readiness_checks.items() if not ready
                    ],
                    "reason": "worker signal summary was not supplied",
                },
                "blockers": [],
                "warnings": ["worker signal summary was not supplied"],
            }
        failed = int(summary.get("failed_count") or 0)
        blocking = int(summary.get("blocking_count") or 0)
        malformed = int(summary.get("malformed_count") or 0)
        recent = int(summary.get("recent_count") or 0)
        blockers: list[str] = []
        if failed:
            blockers.append("one or more worker control signals failed delivery")
        if blocking:
            blockers.append("one or more active worker control signals block live runs")
        readiness_checks = {
            "worker_signal_summary_supplied": True,
            "worker_signal_delivery_not_failed": failed == 0,
            "worker_signals_not_blocking": blocking == 0,
        }
        return {
            "category": "worker_signals",
            "status": "fail" if blockers else "pass",
            "label": "Worker signals",
            "details": {
                "schema_version": "worker_signal_readiness.v1",
                "source_schema_version": summary.get("schema_version"),
                "recent_count": recent,
                "blocking_count": blocking,
                "failed_count": failed,
                "malformed_count": malformed,
                "by_status": summary.get("by_status") or {},
                "by_signal_type": summary.get("by_signal_type") or {},
                "by_target_role": summary.get("by_target_role") or {},
                "active_blocking_target_roles": summary.get("active_blocking_target_roles") or [],
                "by_active_blocking_target_role": summary.get("by_active_blocking_target_role")
                or {},
                "by_delivery_source": summary.get("by_delivery_source") or {},
                "latest_signal": summary.get("latest_signal"),
                "live_readiness_policy": (
                    "failed or blocking worker control signals block live runs"
                ),
                "attention_count": failed + blocking,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": [],
        }

    def _production_run_check(self, summary: dict) -> dict:
        attention = int(summary.get("attention_count") or 0)
        details = self._production_run_details_with_readiness(summary)
        return {
            "name": "production_runs",
            "status": "degraded" if attention else "healthy",
            "details": details
            | {
                "schema_version": "production_run_health.v1",
                "reason": (
                    "production runs need operator attention"
                    if attention
                    else "production runs have no active blockers or paused work"
                ),
            },
        }

    def _production_run_readiness(self, summary: dict) -> dict:
        failed = int(summary.get("failed_active_production_runs") or 0)
        cancelled = int(summary.get("cancelled_active_production_runs") or 0)
        paused = int(summary.get("paused_active_production_runs") or 0)
        running = int(summary.get("running_active_production_runs") or 0)
        completion_blocked = int(summary.get("completion_blocked_production_runs") or 0)
        waiting_for_media = int(summary.get("waiting_for_media_production_runs") or 0)
        waiting_for_action = int(
            summary.get("waiting_for_completion_action_production_runs") or 0
        )
        blockers: list[str] = []
        warnings: list[str] = []
        if failed:
            blockers.append("one or more active production runs are failed")
        if cancelled:
            blockers.append("one or more active production runs are cancelled")
        if completion_blocked:
            blockers.append("one or more production runs are blocked by completion gates")
        if paused:
            warnings.append("one or more active production runs are paused")
        if waiting_for_media:
            warnings.append("one or more production runs are waiting for active media jobs")
        if waiting_for_action:
            warnings.append("one or more production runs are waiting for the next stage or review")
        elif running:
            warnings.append("one or more production runs are already active")
        details = self._production_run_details_with_readiness(summary)
        return {
            "category": "production_runs",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Production runs",
            "details": details
            | {
                "schema_version": "production_run_readiness.v1",
                "live_readiness_policy": (
                    "failed, cancelled, or completion-blocked runs block live runs; "
                    "paused or already-running production runs warn"
                ),
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _production_run_details_with_readiness(self, summary: dict) -> dict:
        readiness_checks = {
            "no_failed_active_production_runs": (
                int(summary.get("failed_active_production_runs") or 0) == 0
            ),
            "no_cancelled_active_production_runs": (
                int(summary.get("cancelled_active_production_runs") or 0) == 0
            ),
            "no_paused_active_production_runs": (
                int(summary.get("paused_active_production_runs") or 0) == 0
            ),
            "no_running_active_production_runs": (
                int(summary.get("running_active_production_runs") or 0) == 0
            ),
            "no_completion_blocked_production_runs": (
                int(summary.get("completion_blocked_production_runs") or 0) == 0
            ),
            "no_production_runs_waiting_for_action": (
                int(summary.get("waiting_for_completion_action_production_runs") or 0) == 0
            ),
        }
        return summary | {
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }

    def _workflow_retry_readiness(self, summary: dict) -> dict:
        exhausted = int(summary.get("exhausted_retry_entries") or 0)
        total = int(summary.get("total_retry_entries") or 0)
        due = int(summary.get("due_retry_entries") or 0)
        backoff = int(summary.get("backoff_retry_entries") or 0)
        unknown = int(summary.get("unknown_schedule_retry_entries") or 0)
        blockers: list[str] = []
        warnings: list[str] = []
        if exhausted:
            blockers.append("one or more workflow stage retries are exhausted")
        if total and not blockers:
            warnings.append("workflow stage retries are scheduled or pending")
        details = self._workflow_retry_details_with_readiness(summary)
        return {
            "category": "workflow_retries",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Workflow retries",
            "details": details
            | {
                "live_readiness_policy": (
                    "exhausted retries block live runs; scheduled retries warn"
                ),
                "pending_retry_entries": total - exhausted,
                "retry_schedule_attention_count": due + backoff + unknown,
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _workflow_retry_details_with_readiness(self, summary: dict) -> dict:
        readiness_checks = {
            "no_exhausted_workflow_retries": (
                int(summary.get("exhausted_retry_entries") or 0) == 0
            ),
            "no_scheduled_workflow_retries": (
                int(summary.get("scheduled_retry_entries") or 0) == 0
            ),
            "no_due_workflow_retries": int(summary.get("due_retry_entries") or 0) == 0,
            "no_backoff_workflow_retries": (int(summary.get("backoff_retry_entries") or 0) == 0),
            "no_unknown_schedule_workflow_retries": (
                int(summary.get("unknown_schedule_retry_entries") or 0) == 0
            ),
        }
        return summary | {
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }

    def _workflow_orchestration_readiness(self, summary: dict) -> dict:
        errors = int(summary.get("current_error_count") or 0)
        blocked = int(summary.get("current_blocked_dispatch_count") or 0)
        blocked_handoffs = int(summary.get("current_blocked_production_handoff_count") or 0)
        waiting_handoffs = int(summary.get("current_waiting_production_handoff_count") or 0)
        waiting_media_handoffs = int(summary.get("current_waiting_media_handoff_count") or 0)
        waiting_action_handoffs = int(summary.get("current_waiting_action_handoff_count") or 0)
        failed_stages = int(summary.get("current_failed_stage_count") or 0)
        blockers: list[str] = []
        warnings: list[str] = []
        if errors:
            blockers.append("workflow orchestration has recorded stage errors")
        if blocked:
            blockers.append("workflow orchestration has blocked Temporal dispatches")
        if blocked_handoffs:
            blockers.append("workflow orchestration has blocked talk-show production handoffs")
        if waiting_media_handoffs:
            warnings.append("workflow orchestration is waiting for active media jobs")
        if waiting_action_handoffs:
            warnings.append(
                "workflow orchestration is waiting for the next production action or review"
            )
        details = self._workflow_orchestration_details_with_readiness(summary)
        return {
            "category": "workflow_orchestration",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Workflow orchestration",
            "details": details
            | {
                "live_readiness_policy": (
                    "current unresolved orchestration errors, blocked dispatches, and "
                    "blocked production handoffs block live runs; historical attempts "
                    "remain visible for audit"
                ),
                "attention_count": (
                    errors + blocked + blocked_handoffs + failed_stages + waiting_handoffs
                ),
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _workflow_orchestration_details_with_readiness(self, summary: dict) -> dict:
        readiness_checks = {
            "no_workflow_orchestration_errors": (
                int(summary.get("current_error_count") or 0) == 0
            ),
            "no_failed_workflow_stages": (
                int(summary.get("current_failed_stage_count") or 0) == 0
            ),
            "no_blocked_temporal_dispatches": (
                int(summary.get("current_blocked_dispatch_count") or 0) == 0
            ),
            "no_blocked_production_handoffs": (
                int(summary.get("current_blocked_production_handoff_count") or 0) == 0
            ),
            "no_production_handoffs_waiting_for_action": (
                int(summary.get("current_waiting_action_handoff_count") or 0) == 0
            ),
        }
        return summary | {
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }

    def _publish_job_readiness(self, summary: dict) -> dict:
        failed = int(summary.get("failed_publish_jobs") or 0)
        submitted = int(summary.get("submitted_publish_jobs") or 0)
        missing_manifests = int(summary.get("packages_missing_production_manifest") or 0)
        invalid_manifests = int(summary.get("invalid_production_manifest_assets") or 0)
        missing_package_qc = int(summary.get("packages_missing_package_qc") or 0)
        failing_package_qc = int(summary.get("packages_failing_package_qc") or 0)
        missing_package_thumbnails = int(summary.get("packages_missing_thumbnail") or 0)
        missing_package_subtitles = int(summary.get("packages_missing_subtitles") or 0)
        blockers: list[str] = []
        warnings: list[str] = []
        if failed:
            blockers.append("one or more publish jobs have failed")
        if missing_package_qc:
            blockers.append("one or more completed export packages are missing package QC")
        if failing_package_qc:
            blockers.append("one or more completed export packages have failing package QC")
        if missing_package_thumbnails:
            blockers.append("one or more completed export packages are missing thumbnails")
        if missing_package_subtitles:
            blockers.append("one or more completed export packages are missing subtitles")
        if submitted:
            warnings.append("one or more publish jobs are still submitted")
        if invalid_manifests:
            warnings.append("one or more production manifest assets are invalid")
        if missing_manifests:
            warnings.append(
                "one or more completed export packages are missing production manifests"
            )
        readiness_checks = {
            "no_failed_publish_jobs": failed == 0,
            "no_packages_missing_package_qc": missing_package_qc == 0,
            "no_packages_failing_package_qc": failing_package_qc == 0,
            "no_packages_missing_thumbnails": missing_package_thumbnails == 0,
            "no_packages_missing_subtitles": missing_package_subtitles == 0,
            "no_submitted_publish_jobs": submitted == 0,
            "no_invalid_production_manifests": invalid_manifests == 0,
            "no_packages_missing_production_manifest": missing_manifests == 0,
        }
        return {
            "category": "publish_jobs",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Publish jobs",
            "details": summary
            | {
                "live_readiness_policy": (
                    "failed publish jobs, missing/failing package QC, or missing package "
                    "thumbnail/subtitle evidence block live runs; submitted jobs and "
                    "invalid or missing production manifests warn"
                ),
                "attention_count": failed
                + missing_package_qc
                + failing_package_qc
                + missing_package_thumbnails
                + missing_package_subtitles
                + submitted
                + invalid_manifests
                + missing_manifests,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _media_queue_readiness(self, summary: dict) -> dict:
        failed = int(summary.get("current_failed_assets") or 0)
        audio_pending = int(summary.get("current_pending_audio_jobs") or 0)
        visual_pending = int(summary.get("current_pending_visual_jobs") or 0)
        subtitle_pending = int(summary.get("current_pending_subtitle_jobs") or 0)
        pending = audio_pending + visual_pending + subtitle_pending
        blockers: list[str] = []
        warnings: list[str] = []
        if failed:
            blockers.append("one or more media assets are failed")
        if pending:
            warnings.append("one or more media jobs are pending or running")
        readiness_checks = {
            "no_failed_media_assets": failed == 0,
            "no_pending_audio_jobs": audio_pending == 0,
            "no_pending_visual_jobs": visual_pending == 0,
            "no_pending_subtitle_jobs": subtitle_pending == 0,
        }
        return {
            "category": "media_queues",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Media queues",
            "details": summary
            | {
                "schema_version": "media_queue_readiness.v1",
                "live_readiness_policy": (
                    "current failed audio, subtitle, and visual media assets block live "
                    "runs; current pending media jobs warn; render attempts have their "
                    "own lifecycle and terminal cancelled or failed episode assets remain "
                    "in historical counts only"
                ),
                "pending_job_count": pending,
                "attention_count": failed + pending,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _live_model_provider_readiness(self, endpoints: list[ModelEndpoint]) -> dict:
        enabled = [endpoint for endpoint in endpoints if endpoint.enabled]
        remote = [
            endpoint
            for endpoint in enabled
            if str(endpoint.provider_type) not in {"mock", "ProviderType.mock"}
        ]
        unhealthy = [
            endpoint for endpoint in enabled if endpoint.health_status in {"unhealthy", "failed"}
        ]
        missing_base_url = [
            endpoint
            for endpoint in remote
            if not endpoint.base_url and str(endpoint.provider_type) != "ollama"
        ]
        by_provider_type: dict[str, int] = {}
        by_health_status: dict[str, int] = {}
        for endpoint in enabled:
            provider_type = str(endpoint.provider_type)
            by_provider_type[provider_type] = by_provider_type.get(provider_type, 0) + 1
            by_health_status[endpoint.health_status] = (
                by_health_status.get(endpoint.health_status, 0) + 1
            )
        blockers: list[str] = []
        warnings: list[str] = []
        if not enabled:
            blockers.append("at least one enabled model endpoint is required")
        if unhealthy:
            blockers.append("one or more enabled model endpoints are unhealthy")
        if missing_base_url:
            blockers.append("one or more remote model endpoints are missing base_url")
        if enabled and not remote:
            warnings.append("only mock model endpoints are enabled")
        unknown = [endpoint for endpoint in enabled if endpoint.health_status == "unknown"]
        if unknown:
            warnings.append("one or more enabled model endpoints have unknown health")
        readiness_checks = {
            "has_enabled_model_endpoint": len(enabled) > 0,
            "has_remote_model_endpoint": len(remote) > 0,
            "remote_model_endpoints_have_base_url": len(missing_base_url) == 0,
            "no_unhealthy_model_endpoints": len(unhealthy) == 0,
            "no_unknown_health_model_endpoints": len(unknown) == 0,
        }
        return {
            "category": "model_providers",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Model providers",
            "details": {
                "configured": len(endpoints),
                "enabled": len(enabled),
                "remote_enabled": len(remote),
                "healthy": sum(1 for endpoint in enabled if endpoint.health_status == "healthy"),
                "unknown": len(unknown),
                "unhealthy": len(unhealthy),
                "missing_base_url": len(missing_base_url),
                "remote_base_url_configured": sum(
                    1 for endpoint in remote if bool(endpoint.base_url)
                ),
                "provider_types": sorted({str(endpoint.provider_type) for endpoint in enabled}),
                "by_provider_type": dict(sorted(by_provider_type.items())),
                "by_health_status": dict(sorted(by_health_status.items())),
                "missing_base_url_endpoints": [
                    self._model_endpoint_readiness_entry(endpoint)
                    for endpoint in missing_base_url[:10]
                ],
                "unhealthy_endpoints": [
                    self._model_endpoint_readiness_entry(endpoint) for endpoint in unhealthy[:10]
                ],
                "unknown_health_endpoints": [
                    self._model_endpoint_readiness_entry(endpoint) for endpoint in unknown[:10]
                ],
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _model_endpoint_readiness_entry(self, endpoint: ModelEndpoint) -> dict:
        return {
            "id": endpoint.id,
            "name": endpoint.name,
            "provider_type": str(endpoint.provider_type),
            "health_status": endpoint.health_status,
            "base_url_configured": bool(endpoint.base_url),
        }

    def _managed_media_smoke_readiness(self) -> dict:
        evidence = managed_media_smoke_evidence(
            self.settings.b1_managed_media_smoke_evidence_path
        )
        status = str(evidence.get("status") or "")
        blockers: list[str] = []
        warnings: list[str] = []
        if evidence.get("configured") is not True:
            warnings.append("B1 managed media smoke evidence path is not configured")
        elif status == "missing":
            blockers.append("B1 managed media smoke has not been run")
        elif status == "busy":
            if evidence.get("fresh") is False:
                warnings.append("B1 managed media busy evidence is stale; rerun it")
            else:
                warnings.append("B1 managed media scheduler is busy; retry the smoke later")
        elif status in {"runner_failed", "fail", "timeout"}:
            if evidence.get("fresh") is False:
                warnings.append("failed B1 managed media smoke evidence is stale; rerun it")
            else:
                blockers.append("latest B1 managed media smoke did not complete successfully")
        elif evidence.get("fresh") is False:
            warnings.append("B1 managed media smoke evidence is stale; rerun it")
        elif evidence.get("ready") is not True:
            warnings.append("latest B1 managed media smoke is not confirmed ready")

        readiness_checks = {
            "managed_media_smoke_evidence_configured": evidence.get("configured") is True,
            "managed_media_smoke_evidence_present": status != "missing",
            "managed_media_smoke_passed": evidence.get("ready") is True,
            "managed_media_smoke_fresh": evidence.get("fresh") is not False,
        }
        return {
            "category": "managed_media_smoke",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "B1 managed media smoke",
            "details": {
                **evidence,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _live_endpoint_readiness(
        self,
        category: str,
        label: str,
        endpoints: list[VoiceboxEndpoint | ComfyUiEndpoint],
        *,
        require_remote_base_url: bool,
    ) -> dict:
        enabled = [endpoint for endpoint in endpoints if endpoint.enabled]
        unhealthy = [
            endpoint for endpoint in enabled if endpoint.health_status in {"unhealthy", "failed"}
        ]
        unknown = [endpoint for endpoint in enabled if endpoint.health_status == "unknown"]
        missing_base_url = [
            endpoint for endpoint in enabled if require_remote_base_url and not endpoint.base_url
        ]
        by_adapter_type: dict[str, int] = {}
        by_health_status: dict[str, int] = {}
        for endpoint in enabled:
            by_adapter_type[endpoint.adapter_type] = (
                by_adapter_type.get(endpoint.adapter_type, 0) + 1
            )
            by_health_status[endpoint.health_status] = (
                by_health_status.get(endpoint.health_status, 0) + 1
            )
        blockers: list[str] = []
        warnings: list[str] = []
        if not enabled:
            blockers.append(f"at least one enabled {label.lower()} record is required")
        if unhealthy:
            blockers.append(f"one or more enabled {label.lower()} records are unhealthy")
        if missing_base_url:
            blockers.append(f"one or more enabled {label.lower()} records are missing base_url")
        if unknown:
            warnings.append(f"one or more enabled {label.lower()} records have unknown health")
        gate_prefix = category.replace("-", "_")
        readiness_checks = {
            f"has_enabled_{gate_prefix}_endpoint": len(enabled) > 0,
            f"{gate_prefix}_endpoints_have_base_url": len(missing_base_url) == 0,
            f"no_unhealthy_{gate_prefix}_endpoints": len(unhealthy) == 0,
            f"no_unknown_health_{gate_prefix}_endpoints": len(unknown) == 0,
        }
        return {
            "category": category,
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": label,
            "details": {
                "configured": len(endpoints),
                "enabled": len(enabled),
                "healthy": sum(1 for endpoint in enabled if endpoint.health_status == "healthy"),
                "unknown": len(unknown),
                "unhealthy": len(unhealthy),
                "missing_base_url": len(missing_base_url),
                "remote_base_url_configured": sum(
                    1 for endpoint in enabled if bool(endpoint.base_url)
                ),
                "require_remote_base_url": require_remote_base_url,
                "by_adapter_type": dict(sorted(by_adapter_type.items())),
                "by_health_status": dict(sorted(by_health_status.items())),
                "missing_base_url_endpoints": [
                    self._media_endpoint_readiness_entry(endpoint)
                    for endpoint in missing_base_url[:10]
                ],
                "unhealthy_endpoints": [
                    self._media_endpoint_readiness_entry(endpoint) for endpoint in unhealthy[:10]
                ],
                "unknown_health_endpoints": [
                    self._media_endpoint_readiness_entry(endpoint) for endpoint in unknown[:10]
                ],
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _media_endpoint_readiness_entry(self, endpoint: VoiceboxEndpoint | ComfyUiEndpoint) -> dict:
        entry = {
            "id": endpoint.id,
            "name": endpoint.name,
            "adapter_type": endpoint.adapter_type,
            "health_status": endpoint.health_status,
            "base_url_configured": bool(endpoint.base_url),
        }
        prompt_admission = self._prompt_admission_readiness_entry(endpoint)
        if prompt_admission:
            entry["prompt_admission"] = prompt_admission
        voice_generation = self._voice_generation_readiness_entry(endpoint)
        if voice_generation:
            entry["voice_generation"] = voice_generation
        managed_media = self._managed_media_readiness_entry(endpoint)
        if managed_media:
            entry["managed_media"] = managed_media
        return entry

    def _voice_generation_readiness_entry(
        self,
        endpoint: VoiceboxEndpoint | ComfyUiEndpoint,
    ) -> dict[str, object]:
        if not isinstance(endpoint, VoiceboxEndpoint):
            return {}
        capabilities = endpoint.capabilities if isinstance(endpoint.capabilities, dict) else {}
        canary = capabilities.get("generation_canary")
        if not isinstance(canary, dict):
            return {}
        status = str(canary.get("status") or "")
        return {
            "ready": status in {"pass", "busy"},
            "status": status or None,
            "status_code": canary.get("status_code"),
            "content_type": canary.get("content_type"),
            "bytes": canary.get("bytes"),
            "riff_wave": canary.get("riff_wave"),
            "profile_id": canary.get("profile_id"),
            "engine": canary.get("engine"),
            "error_type": canary.get("error_type"),
            "action": self._voice_generation_operator_action(status),
        }

    def _voice_generation_operator_action(self, status: str) -> str:
        if status == "pass":
            return "voice_generation_ready"
        if status == "busy":
            return "wait_for_b1_gpu_scheduler_then_retry_voice_generation"
        if status == "skipped":
            return "configure_voice_generation_canary"
        if status == "fail":
            return "fix_voicebox_generation_then_rerun_health_check"
        return "inspect_voicebox_generation_health"

    def _prompt_admission_readiness_entry(
        self,
        endpoint: VoiceboxEndpoint | ComfyUiEndpoint,
    ) -> dict[str, object]:
        if not isinstance(endpoint, ComfyUiEndpoint):
            return {}
        capabilities = endpoint.capabilities if isinstance(endpoint.capabilities, dict) else {}
        probe = capabilities.get("prompt_admission_probe")
        if not isinstance(probe, dict):
            return {}
        response = probe.get("response")
        response_detail = response.get("detail") if isinstance(response, dict) else None
        detail = response_detail if isinstance(response_detail, dict) else {}
        hardware_policy = detail.get("hardware_resource_policy")
        hardware_detail = (
            hardware_policy.get("detail") if isinstance(hardware_policy, dict) else None
        )
        return {
            "ready": capabilities.get("prompt_admission_ready") is True,
            "status_code": probe.get("status_code"),
            "code": detail.get("code"),
            "message": detail.get("message"),
            "detail": hardware_detail,
        }

    def _managed_media_readiness_entry(self, endpoint: ComfyUiEndpoint) -> dict[str, object]:
        capabilities = endpoint.capabilities if isinstance(endpoint.capabilities, dict) else {}
        if "managed_media_available_presets" not in capabilities:
            return {}
        return {
            "ready": capabilities.get("managed_media_catalog_ready") is True,
            "api": capabilities.get("managed_media_api") is True,
            "status_code": capabilities.get("managed_media_catalog_status_code"),
            "model_count": capabilities.get("managed_media_model_count"),
            "required_presets": capabilities.get("managed_media_required_presets", []),
            "available_presets": capabilities.get("managed_media_available_presets", []),
            "missing_presets": capabilities.get("managed_media_missing_presets", []),
        }

    def _automated_live_publisher_targets(
        self,
        targets: list[PublisherTarget],
    ) -> list[PublisherTarget]:
        return [
            target
            for target in targets
            if target.enabled
            and target.adapter_type != "mock"
            and target.capabilities.get("dry_run_only") is not True
            and target.capabilities.get("automated_live_publish") is True
        ]

    def _publisher_readiness(self, targets: list[PublisherTarget]) -> dict:
        enabled = [target for target in targets if target.enabled]
        unhealthy = [
            target for target in enabled if target.health_status in {"unhealthy", "failed"}
        ]
        live_targets = [
            target
            for target in enabled
            if target.adapter_type != "mock" and target.capabilities.get("dry_run_only") is not True
        ]
        automated_live_targets = self._automated_live_publisher_targets(targets)
        breakdowns = self._publisher_target_breakdowns(enabled)
        automated_live_enabled = self.settings.publisher_automated_live_enabled
        blockers: list[str] = []
        warnings: list[str] = []
        if unhealthy:
            blockers.append("one or more enabled publisher targets are unhealthy")
        if automated_live_enabled and not automated_live_targets:
            blockers.append(
                "automated live publishing is enabled but no enabled target declares "
                "automated_live_publish"
            )
        if not enabled:
            warnings.append("no publisher targets are enabled; direct publishing is optional")
        elif not live_targets:
            warnings.append("only dry-run or mock publisher targets are enabled")
        unknown = [target for target in enabled if target.health_status == "unknown"]
        if unknown:
            warnings.append("one or more enabled publisher targets have unknown health")
        readiness_checks = self._publisher_target_readiness_checks(
            enabled=enabled,
            live_targets=live_targets,
            automated_live_targets=automated_live_targets,
            automated_live_enabled=automated_live_enabled,
            unhealthy=unhealthy,
            unknown=unknown,
        )
        return {
            "category": "publisher_targets",
            "status": "fail" if blockers else "warning" if warnings else "pass",
            "label": "Publisher targets",
            "details": {
                "configured": len(targets),
                "enabled": len(enabled),
                "live_enabled": len(live_targets),
                "automated_live_enabled": automated_live_enabled,
                "automated_live_capable_enabled": len(automated_live_targets),
                "mock_enabled": sum(1 for target in enabled if target.adapter_type == "mock"),
                "dry_run_only_enabled": sum(
                    1 for target in enabled if target.capabilities.get("dry_run_only") is True
                ),
                "healthy": sum(1 for target in enabled if target.health_status == "healthy"),
                "unknown": len(unknown),
                "unhealthy": len(unhealthy),
                "by_adapter_type": breakdowns["by_adapter_type"],
                "by_health_status": breakdowns["by_health_status"],
                "by_platform": breakdowns["by_platform"],
                "live_readiness_policy": (
                    "automated live publishing requires an enabled live target with "
                    "automated_live_publish"
                ),
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
            "blockers": blockers,
            "warnings": warnings,
        }

    def _publisher_target_check(self, targets: list[PublisherTarget]) -> dict:
        enabled = [target for target in targets if target.enabled]
        unhealthy = [
            target for target in enabled if target.health_status in {"unhealthy", "failed"}
        ]
        unknown = [target for target in enabled if target.health_status == "unknown"]
        live_targets = [
            target
            for target in enabled
            if target.adapter_type != "mock" and target.capabilities.get("dry_run_only") is not True
        ]
        automated_live_targets = self._automated_live_publisher_targets(targets)
        breakdowns = self._publisher_target_breakdowns(enabled)
        automated_live_enabled = self.settings.publisher_automated_live_enabled
        issues: list[str] = []
        if unhealthy:
            issues.append("one or more enabled publisher targets are unhealthy")
        if automated_live_enabled and not automated_live_targets:
            issues.append(
                "automated live publishing is enabled but no enabled target declares "
                "automated_live_publish"
            )
        if unknown:
            issues.append("one or more enabled publisher targets have unknown health")
        reason = "publisher target configuration is operator-ready"
        if not enabled:
            reason = "no publisher targets are enabled; direct publishing is optional"
        elif issues:
            reason = "; ".join(issues)
        elif not live_targets:
            reason = "only dry-run or mock publisher targets are enabled"
        readiness_checks = self._publisher_target_readiness_checks(
            enabled=enabled,
            live_targets=live_targets,
            automated_live_targets=automated_live_targets,
            automated_live_enabled=automated_live_enabled,
            unhealthy=unhealthy,
            unknown=unknown,
        )
        return {
            "name": "publisher_targets",
            "status": "degraded" if issues else "healthy",
            "details": {
                "schema_version": "publisher_target_health.v1",
                "configured": len(targets),
                "enabled": len(enabled),
                "live_enabled": len(live_targets),
                "automated_live_enabled": automated_live_enabled,
                "automated_live_capable_enabled": len(automated_live_targets),
                "mock_enabled": sum(1 for target in enabled if target.adapter_type == "mock"),
                "dry_run_only_enabled": sum(
                    1 for target in enabled if target.capabilities.get("dry_run_only") is True
                ),
                "healthy": sum(1 for target in enabled if target.health_status == "healthy"),
                "unknown": len(unknown),
                "unhealthy": len(unhealthy),
                "by_adapter_type": breakdowns["by_adapter_type"],
                "by_health_status": breakdowns["by_health_status"],
                "by_platform": breakdowns["by_platform"],
                "issue_count": len(issues),
                "issues": issues,
                "reason": reason,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
        }

    def _publisher_target_readiness_checks(
        self,
        *,
        enabled: list[PublisherTarget],
        live_targets: list[PublisherTarget],
        automated_live_targets: list[PublisherTarget],
        automated_live_enabled: bool,
        unhealthy: list[PublisherTarget],
        unknown: list[PublisherTarget],
    ) -> dict[str, bool]:
        return {
            "has_enabled_publisher_target": len(enabled) > 0,
            "has_live_publisher_target": len(live_targets) > 0,
            "automated_live_target_available": (
                not automated_live_enabled or len(automated_live_targets) > 0
            ),
            "no_unhealthy_publisher_targets": len(unhealthy) == 0,
            "no_unknown_health_publisher_targets": len(unknown) == 0,
        }

    def _publisher_target_breakdowns(self, targets: list[PublisherTarget]) -> dict:
        by_adapter_type: dict[str, int] = {}
        by_health_status: dict[str, int] = {}
        by_platform: dict[str, int] = {}
        for target in targets:
            by_adapter_type[target.adapter_type] = by_adapter_type.get(target.adapter_type, 0) + 1
            by_health_status[target.health_status] = (
                by_health_status.get(target.health_status, 0) + 1
            )
            by_platform[target.platform] = by_platform.get(target.platform, 0) + 1
        return {
            "by_adapter_type": dict(sorted(by_adapter_type.items())),
            "by_health_status": dict(sorted(by_health_status.items())),
            "by_platform": dict(sorted(by_platform.items())),
        }

    def _object_storage_check(self) -> dict:
        backend = self.settings.object_storage_backend.strip().lower()
        if backend in {"local", "local_object_store", "filesystem"}:
            root = Path(self.settings.object_storage_local_path).expanduser()
            existing = root if root.exists() else root.parent
            existing_is_dir = existing.exists() and existing.is_dir()
            writable = existing_is_dir and self._path_writable(existing)
            reason = "local object-storage target or parent is writable"
            if not existing.exists():
                reason = "local object-storage path or parent directory does not exist"
            elif not existing_is_dir:
                reason = "local object-storage checked path is not a directory"
            elif not writable:
                reason = "local object-storage path or parent directory is not writable"
            readiness_checks = {
                "checked_path_exists": existing.exists(),
                "checked_path_is_directory": existing_is_dir,
                "writable_target_or_parent": writable,
            }
            return {
                "name": "object_storage",
                "status": "healthy" if writable else "degraded",
                "details": {
                    "backend": self.settings.object_storage_backend,
                    "bucket": self.settings.object_storage_bucket,
                    "local_path": str(root),
                    "path_exists": root.exists(),
                    "parent_exists": root.parent.exists(),
                    "checked_path": str(existing),
                    "checked_path_exists": existing.exists(),
                    "checked_path_is_dir": existing_is_dir,
                    "writable_target_or_parent": writable,
                    "writable_parent": writable,
                    "readiness_checks": readiness_checks,
                    "failed_readiness_checks": [
                        name for name, ready in readiness_checks.items() if not ready
                    ],
                    "reason": reason,
                },
            }
        if backend in {"s3", "s3-compatible", "minio"}:
            probe = self._object_storage_endpoint_tcp_probe(self.settings.object_storage_endpoint)
            access_key_reference_configured = self._configured_string(
                self.settings.object_storage_access_key_reference
            )
            secret_key_reference_configured = self._configured_string(
                self.settings.object_storage_secret_key_reference
            )
            credentials_ready = (
                access_key_reference_configured == secret_key_reference_configured
            )
            bucket_configured = bool(self.settings.object_storage_bucket.strip())
            bucket_probe = (
                self._object_storage_bucket_probe()
                if probe["reachable"] and credentials_ready and bucket_configured
                else {"available": False, "skipped": True}
            )
            bucket_available = bucket_probe.get("available") is True
            ready = (
                probe["reachable"] and credentials_ready and bucket_configured and bucket_available
            )
            reason = "S3-compatible object storage endpoint is reachable"
            if not probe["reachable"]:
                reason = "S3-compatible object storage endpoint is not reachable"
            elif not credentials_ready:
                reason = "S3 access and secret key references must both be configured"
            elif not bucket_configured:
                reason = "S3 bucket name is not configured"
            elif not bucket_available:
                reason = "S3 bucket is not reachable with configured credentials"
            else:
                reason = "S3-compatible object storage bucket is reachable"
            readiness_checks = {
                "endpoint_reachable": probe["reachable"],
                "credential_pair_configured": credentials_ready,
                "bucket_configured": bucket_configured,
                "bucket_available": bucket_available,
            }
            return {
                "name": "object_storage",
                "status": "healthy" if ready else "degraded",
                "details": {
                    "backend": self.settings.object_storage_backend,
                    "bucket": self.settings.object_storage_bucket,
                    "endpoint": self.settings.object_storage_endpoint,
                    "region": self.settings.object_storage_region,
                    "force_path_style": self.settings.object_storage_force_path_style,
                    "auto_create_bucket": self.settings.object_storage_auto_create_bucket,
                    "access_key_reference_configured": access_key_reference_configured,
                    "secret_key_reference_configured": secret_key_reference_configured,
                    "credentials_ready": credentials_ready,
                    "bucket_configured": bucket_configured,
                    "bucket_available": bucket_available,
                    "tcp_probe": probe,
                    "bucket_probe": bucket_probe,
                    "readiness_checks": readiness_checks,
                    "failed_readiness_checks": [
                        name for name, ready in readiness_checks.items() if not ready
                    ],
                    "reason": reason,
                },
            }
        return {
            "name": "object_storage",
            "status": "unhealthy",
            "details": {
                "backend": self.settings.object_storage_backend,
                "bucket": self.settings.object_storage_bucket,
                "readiness_checks": {"backend_supported": False},
                "failed_readiness_checks": ["backend_supported"],
                "reason": "unsupported object storage backend",
            },
        }

    def _deployment_readiness_check(
        self,
        publisher_targets: list[PublisherTarget] | None = None,
        model_endpoints: list[ModelEndpoint] | None = None,
        voicebox_endpoints: list[VoiceboxEndpoint] | None = None,
        comfyui_endpoints: list[ComfyUiEndpoint] | None = None,
    ) -> dict:
        env = self.settings.env.strip().lower()
        production = env == "production"
        database_resolution_error: str | None = None
        try:
            database_url = self.settings.resolved_database_url()
            database_driver = database_url.split(":", maxsplit=1)[0]
        except RuntimeError as exc:
            database_url = ""
            database_driver = self.settings.database_driver.strip().split(":", maxsplit=1)[0]
            database_resolution_error = str(exc)
        object_storage_backend = self.settings.object_storage_backend.strip().lower()
        temporal_mode = self.settings.temporal_backend_mode.strip().lower() or "local"
        runtime_paths = self._runtime_paths_summary()
        temporal_runtime_contract = self._temporal_runtime_contract_readiness(temporal_mode)
        database_migration_details = self._database_migrations_check()["details"]
        database_migration_checks = database_migration_details.get("readiness_checks", {})
        database_migration_check_available = (
            database_migration_checks.get("migration_revision_check_available") is True
        )
        database_schema_at_head = database_migration_checks.get("database_schema_at_head") is True
        unsafe_default_secret_labels = self._unsafe_default_secret_labels()
        auth_api_key_reference_status = self._auth_api_key_reference_status()
        auth_api_key_reference_available = auth_api_key_reference_status["status"] in {
            "disabled",
            "resolved",
        }
        automated_live_targets = self._automated_live_publisher_targets(publisher_targets or [])
        publisher_target_summary = self._publisher_target_deployment_summary(
            publisher_targets or []
        )
        model_provider_summary = self._model_provider_deployment_summary(model_endpoints or [])
        voicebox_summary = self._media_provider_deployment_summary(voicebox_endpoints or [])
        comfyui_summary = self._media_provider_deployment_summary(comfyui_endpoints or [])
        checks = {
            "production_mode": production,
            "database_driver": database_driver,
            "database_url_resolved": database_resolution_error is None,
            "database_persistent": database_driver not in {"sqlite", "sqlite+pysqlite"},
            "database_schema_at_head": (
                not database_migration_check_available or database_schema_at_head
            ),
            "cors_origin_restricted": "*" not in self.settings.resolved_cors_allowed_origins(),
            "auth_enabled": self.settings.auth_enabled,
            "auth_mode_configured": self._auth_mode_configured(),
            "auth_api_key_reference_available": auth_api_key_reference_available,
            "initial_admin_path_configured": (
                not self.settings.auth_enabled or self._initial_admin_path_configured()
            ),
            "object_storage_remote": object_storage_backend in {"s3", "s3-compatible", "minio"},
            "object_storage_endpoint_configured": (
                object_storage_backend not in {"s3", "s3-compatible", "minio"}
                or bool(self.settings.object_storage_endpoint.strip())
            ),
            "object_storage_bucket_configured": (
                object_storage_backend not in {"s3", "s3-compatible", "minio"}
                or bool(self.settings.object_storage_bucket.strip())
            ),
            "object_storage_credential_pair_configured": (
                object_storage_backend not in {"s3", "s3-compatible", "minio"}
                or self._configured_string(self.settings.object_storage_access_key_reference)
                == self._configured_string(self.settings.object_storage_secret_key_reference)
            ),
            "redis_runtime_enabled": (
                self.settings.redis_event_fanout_enabled
                and self.settings.redis_worker_signal_enabled
            ),
            "redis_url_configured": bool(self.settings.redis_url.strip()),
            "redis_runtime_channels_configured": (
                (
                    not self.settings.redis_event_fanout_enabled
                    or bool(self.settings.redis_event_channel.strip())
                )
                and (
                    not self.settings.redis_worker_signal_enabled
                    or bool(self.settings.redis_worker_signal_stream.strip())
                )
            ),
            "worker_heartbeat_ttl_covers_poll_interval": (
                self.settings.worker_heartbeat_ttl_seconds
                > self.settings.worker_poll_interval_seconds
            ),
            "worker_lease_ttl_covers_poll_interval": (
                self.settings.worker_lease_ttl_seconds > self.settings.worker_poll_interval_seconds
            ),
            "backup_path_configured": bool(self.settings.backup_path.strip()),
            "runtime_state_path_configured": bool(self.settings.runtime_state_path.strip()),
            "backup_path_writable": bool(
                runtime_paths.get("backup", {}).get("writable_target_or_parent")
            ),
            "runtime_state_path_writable": bool(
                runtime_paths.get("runtime_state", {}).get("writable_target_or_parent")
            ),
            "runtime_paths_free_space_sufficient": all(
                bool(item.get("free_bytes_sufficient"))
                for item in runtime_paths.values()
                if item.get("required") is True
            ),
            "object_storage_local_path_writable": (
                bool(runtime_paths.get("object_storage_local", {}).get("writable_target_or_parent"))
                if object_storage_backend == "local"
                else True
            ),
            "unsafe_default_secrets_replaced": not unsafe_default_secret_labels,
            "temporal_runtime_contract_valid": temporal_runtime_contract["valid_mode"],
            "temporal_runtime_contract_configured": temporal_runtime_contract["configured"],
            "publisher_automated_live_target_available": (
                not self.settings.publisher_automated_live_enabled
                or len(automated_live_targets) > 0
            ),
            "publisher_target_enabled": publisher_target_summary["enabled"] > 0,
            "publisher_live_target_enabled": publisher_target_summary["live_enabled"] > 0,
            "publisher_targets_not_unhealthy": publisher_target_summary["unhealthy"] == 0,
            "publisher_target_health_known": publisher_target_summary["unknown"] == 0,
            "model_provider_endpoint_enabled": model_provider_summary["enabled"] > 0,
            "model_provider_remote_endpoint_enabled": (
                model_provider_summary["remote_enabled"] > 0
            ),
            "model_provider_remote_endpoint_configured": (
                model_provider_summary["missing_base_url"] == 0
            ),
            "model_provider_endpoints_not_unhealthy": (model_provider_summary["unhealthy"] == 0),
            "model_provider_endpoint_health_known": (model_provider_summary["unknown"] == 0),
            "voicebox_endpoint_enabled": voicebox_summary["enabled"] > 0,
            "voicebox_remote_endpoint_enabled": voicebox_summary["remote_enabled"] > 0,
            "voicebox_remote_endpoint_configured": (voicebox_summary["missing_base_url"] == 0),
            "voicebox_endpoints_not_unhealthy": voicebox_summary["unhealthy"] == 0,
            "voicebox_endpoint_health_known": voicebox_summary["unknown"] == 0,
            "comfyui_endpoint_enabled": comfyui_summary["enabled"] > 0,
            "comfyui_remote_endpoint_enabled": comfyui_summary["remote_enabled"] > 0,
            "comfyui_remote_endpoint_configured": (comfyui_summary["missing_base_url"] == 0),
            "comfyui_endpoints_not_unhealthy": comfyui_summary["unhealthy"] == 0,
            "comfyui_endpoint_health_known": comfyui_summary["unknown"] == 0,
            "temporal_mode": temporal_mode,
        }
        issues: list[str] = []
        if production and database_resolution_error:
            issues.append("database credential reference could not be resolved")
        if production and not checks["database_persistent"]:
            issues.append("production deployments should not use sqlite")
        if production and not checks["database_schema_at_head"]:
            issues.append("production database schema must match the current Alembic head")
        if production and not checks["cors_origin_restricted"]:
            issues.append("production API CORS origins should be restricted")
        if production and not self.settings.auth_enabled:
            issues.append("production deployments should enable authentication")
        if production and not checks["auth_mode_configured"]:
            issues.append("production authentication needs at least one configured auth mode")
        if production and not checks["auth_api_key_reference_available"]:
            issues.append("configured API-key reference is unavailable")
        if production and not checks["initial_admin_path_configured"]:
            issues.append("production authentication needs an admin-capable bootstrap path")
        if production and not checks["object_storage_remote"]:
            issues.append("production media storage should use S3-compatible object storage")
        if production and not checks["object_storage_endpoint_configured"]:
            issues.append("production object storage needs an S3-compatible endpoint")
        if production and not checks["object_storage_bucket_configured"]:
            issues.append("production object storage needs a bucket name")
        if production and not checks["object_storage_credential_pair_configured"]:
            issues.append(
                "production object storage access and secret key references "
                "must be configured together"
            )
        if production and not checks["redis_runtime_enabled"]:
            issues.append("production workers should enable Redis fan-out and worker signals")
        if production and not checks["redis_url_configured"]:
            issues.append("production Redis runtime needs DIALECTICORE_REDIS_URL")
        if production and not checks["redis_runtime_channels_configured"]:
            issues.append(
                "production Redis runtime needs event channel and worker signal stream names"
            )
        if production and not checks["worker_heartbeat_ttl_covers_poll_interval"]:
            issues.append("worker heartbeat TTL should be greater than worker poll interval")
        if production and not checks["worker_lease_ttl_covers_poll_interval"]:
            issues.append("worker lease TTL should be greater than worker poll interval")
        if production and not checks["backup_path_configured"]:
            issues.append("backup path is not configured")
        if production and not checks["runtime_state_path_configured"]:
            issues.append("runtime state path is not configured")
        if production and not checks["backup_path_writable"]:
            issues.append("backup path or parent directory is not writable")
        if production and not checks["runtime_state_path_writable"]:
            issues.append("runtime state path or parent directory is not writable")
        if production and not checks["runtime_paths_free_space_sufficient"]:
            issues.append("one or more required runtime paths are below the free-space floor")
        if production and not checks["object_storage_local_path_writable"]:
            issues.append("local object storage path or parent directory is not writable")
        if production and not checks["unsafe_default_secrets_replaced"]:
            issues.append("production deployments must replace placeholder/default secrets")
        if production and not temporal_runtime_contract["valid_mode"]:
            issues.append("Temporal backend mode must be local, bridge, or external")
        elif production and not temporal_runtime_contract["configured"]:
            issues.append(str(temporal_runtime_contract["reason"]))
        if production and not checks["publisher_automated_live_target_available"]:
            issues.append("automated live publishing needs an enabled live publisher target")
        if production and not checks["publisher_target_enabled"]:
            issues.append("production publishing needs an enabled publisher target")
        if production and not checks["publisher_live_target_enabled"]:
            issues.append(
                "production publishing needs an enabled non-mock, non-dry-run publisher target"
            )
        if production and not checks["publisher_targets_not_unhealthy"]:
            issues.append("production publisher targets must not be unhealthy")
        if production and not checks["publisher_target_health_known"]:
            issues.append("production publisher targets must have known health")
        if production and not checks["model_provider_endpoint_enabled"]:
            issues.append("production model routing needs an enabled model endpoint")
        if production and not checks["model_provider_remote_endpoint_enabled"]:
            issues.append("production model routing needs an enabled non-mock model endpoint")
        if production and not checks["model_provider_remote_endpoint_configured"]:
            issues.append("production remote model endpoints need a configured base URL")
        if production and not checks["model_provider_endpoints_not_unhealthy"]:
            issues.append("production model endpoints must not be unhealthy")
        if production and not checks["model_provider_endpoint_health_known"]:
            issues.append("production model endpoints must have known health")
        if production and not checks["voicebox_endpoint_enabled"]:
            issues.append("production audio synthesis needs an enabled Voicebox endpoint")
        if production and not checks["voicebox_remote_endpoint_enabled"]:
            issues.append("production audio synthesis needs an enabled non-mock Voicebox endpoint")
        if production and not checks["voicebox_remote_endpoint_configured"]:
            issues.append("production Voicebox endpoints need a configured base URL")
        if production and not checks["voicebox_endpoints_not_unhealthy"]:
            issues.append("production Voicebox endpoints must not be unhealthy")
        if production and not checks["voicebox_endpoint_health_known"]:
            issues.append("production Voicebox endpoints must have known health")
        if production and not checks["comfyui_endpoint_enabled"]:
            issues.append("production visual generation needs an enabled ComfyUI endpoint")
        if production and not checks["comfyui_remote_endpoint_enabled"]:
            issues.append("production visual generation needs an enabled non-mock ComfyUI endpoint")
        if production and not checks["comfyui_remote_endpoint_configured"]:
            issues.append("production ComfyUI endpoints need a configured base URL")
        if production and not checks["comfyui_endpoints_not_unhealthy"]:
            issues.append("production ComfyUI endpoints must not be unhealthy")
        if production and not checks["comfyui_endpoint_health_known"]:
            issues.append("production ComfyUI endpoints must have known health")
        readiness_checks = {
            name: ready
            for name, ready in checks.items()
            if isinstance(ready, bool) and name != "production_mode"
        }
        return {
            "name": "deployment_readiness",
            "status": "degraded" if issues else "healthy",
            "details": {
                "schema_version": "deployment_readiness.v1",
                "env": self.settings.env,
                "production_mode": production,
                "database_driver": database_driver,
                "database_resolution_error": database_resolution_error,
                "database_migrations": database_migration_details,
                "object_storage_backend": self.settings.object_storage_backend,
                "temporal_backend_mode": self.settings.temporal_backend_mode,
                "temporal_runtime_contract": temporal_runtime_contract,
                "publisher_automated_live_enabled": (
                    self.settings.publisher_automated_live_enabled
                ),
                "publisher_automated_live_capable_enabled": len(automated_live_targets),
                "publisher_target_summary": publisher_target_summary,
                "model_provider_summary": model_provider_summary,
                "voicebox_summary": voicebox_summary,
                "comfyui_summary": comfyui_summary,
                "auth_api_key_reference_status": auth_api_key_reference_status,
                "runtime_paths": runtime_paths,
                "unsafe_default_secret_labels": unsafe_default_secret_labels,
                "issue_count": len(issues),
                "issues": issues,
                "checks": checks,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
                "reason": (
                    "; ".join(issues)
                    if issues
                    else (
                        "production deployment readiness checks passed"
                        if production
                        else "development deployment readiness checks are informational"
                    )
                ),
            },
        }

    def _model_provider_deployment_summary(self, endpoints: list[ModelEndpoint]) -> dict:
        enabled = [endpoint for endpoint in endpoints if endpoint.enabled]
        remote = [
            endpoint
            for endpoint in enabled
            if str(endpoint.provider_type) not in {"mock", "ProviderType.mock"}
        ]
        missing_base_url = [
            endpoint
            for endpoint in remote
            if not endpoint.base_url and str(endpoint.provider_type) != "ollama"
        ]
        unhealthy = [
            endpoint for endpoint in enabled if endpoint.health_status in {"unhealthy", "failed"}
        ]
        unknown = [endpoint for endpoint in enabled if endpoint.health_status == "unknown"]
        return {
            "configured": len(endpoints),
            "enabled": len(enabled),
            "remote_enabled": len(remote),
            "missing_base_url": len(missing_base_url),
            "unhealthy": len(unhealthy),
            "unknown": len(unknown),
        }

    def _media_provider_deployment_summary(
        self,
        endpoints: list[VoiceboxEndpoint | ComfyUiEndpoint],
    ) -> dict:
        enabled = [endpoint for endpoint in endpoints if endpoint.enabled]
        remote = [
            endpoint for endpoint in enabled if endpoint.adapter_type.strip().lower() != "mock"
        ]
        missing_base_url = [endpoint for endpoint in remote if not endpoint.base_url]
        unhealthy = [
            endpoint for endpoint in enabled if endpoint.health_status in {"unhealthy", "failed"}
        ]
        unknown = [endpoint for endpoint in enabled if endpoint.health_status == "unknown"]
        return {
            "configured": len(endpoints),
            "enabled": len(enabled),
            "remote_enabled": len(remote),
            "missing_base_url": len(missing_base_url),
            "unhealthy": len(unhealthy),
            "unknown": len(unknown),
        }

    def _publisher_target_deployment_summary(self, targets: list[PublisherTarget]) -> dict:
        enabled = [target for target in targets if target.enabled]
        live_targets = [
            target
            for target in enabled
            if target.adapter_type != "mock" and target.capabilities.get("dry_run_only") is not True
        ]
        unhealthy = [
            target for target in enabled if target.health_status in {"unhealthy", "failed"}
        ]
        unknown = [target for target in enabled if target.health_status == "unknown"]
        return {
            "configured": len(targets),
            "enabled": len(enabled),
            "live_enabled": len(live_targets),
            "unhealthy": len(unhealthy),
            "unknown": len(unknown),
        }

    def _unsafe_default_secret_labels(self) -> list[str]:
        unsafe: set[str] = set()
        sentinel_env_values = {
            "DIALECTICORE_API_KEY": {"change-me-before-enabling-auth"},
            "MINIO_ROOT_USER": {"dialecticore"},
            "MINIO_ROOT_PASSWORD": {"change-me-in-production"},
            "POSTGRES_PASSWORD": {"dialecticore"},
        }
        sentinel_reference_values = {
            "DIALECTICORE_AUTH_API_KEY_REFERENCE": {"change-me-before-enabling-auth"},
            "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE": {"change-me-in-production"},
            "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE": {"dialecticore"},
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE": {"dialecticore"},
        }
        configured_references = {
            "DIALECTICORE_AUTH_API_KEY_REFERENCE": self.settings.auth_api_key_reference,
            "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE": (
                self.settings.object_storage_secret_key_reference
            ),
            "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE": (
                self.settings.object_storage_access_key_reference
            ),
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE": (self.settings.database_password_reference),
        }
        for name, sentinel_values in sentinel_env_values.items():
            value = os.environ.get(name)
            if value is not None and value.strip() in sentinel_values:
                unsafe.add(name)
        for name, sentinel_values in sentinel_reference_values.items():
            reference = configured_references[name]
            if self._credential_reference_resolves_to_any(reference, sentinel_values):
                unsafe.add(name)

        try:
            parsed = urlparse(self.settings.resolved_database_url())
        except (RuntimeError, ValueError):
            parsed = None
        if parsed is not None and parsed.password == "dialecticore":
            unsafe.add("DIALECTICORE_DATABASE_URL.password")

        return sorted(unsafe)

    def _credential_reference_resolves_to_any(
        self,
        reference: str | None,
        sentinel_values: set[str],
    ) -> bool:
        if not reference:
            return False
        try:
            value = self.secret_resolver.resolve(reference)
        except RuntimeError:
            return False
        return value is not None and value.strip() in sentinel_values

    def _temporal_runtime_contract_readiness(self, mode: str) -> dict:
        if mode not in {"local", "bridge", "external"}:
            return {
                "mode": mode,
                "valid_mode": False,
                "configured": False,
                "missing": ["DIALECTICORE_TEMPORAL_BACKEND_MODE"],
                "reason": "Temporal backend mode must be local, bridge, or external",
            }
        if mode == "local":
            return {
                "mode": mode,
                "valid_mode": True,
                "configured": True,
                "missing": [],
                "reason": "local workflow control contract is selected",
            }
        if mode == "bridge":
            missing = []
            if not self.settings.temporal_signal_transport_enabled:
                missing.append("DIALECTICORE_TEMPORAL_SIGNAL_TRANSPORT_ENABLED")
            if not self._configured_string(self.settings.temporal_signal_endpoint):
                missing.append("DIALECTICORE_TEMPORAL_SIGNAL_ENDPOINT")
            return {
                "mode": mode,
                "valid_mode": True,
                "configured": not missing,
                "missing": missing,
                "reason": (
                    "bridge Temporal mode needs signal transport and endpoint settings"
                    if missing
                    else "bridge Temporal signal transport is configured"
                ),
            }
        missing = []
        if not self._configured_string(self.settings.temporal_backend_address):
            missing.append("DIALECTICORE_TEMPORAL_BACKEND_ADDRESS")
        if not self._configured_string(self.settings.temporal_task_queue):
            missing.append("DIALECTICORE_TEMPORAL_TASK_QUEUE")
        if not self.settings.temporal_backend_worker_enabled:
            missing.append("DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED")
        return {
            "mode": mode,
            "valid_mode": True,
            "configured": not missing,
            "missing": missing,
            "reason": (
                "external Temporal mode needs backend address, task queue, "
                "and native worker enabled"
                if missing
                else "external Temporal backend settings are configured"
            ),
        }

    def _runtime_paths_check(self) -> dict:
        summary = self._runtime_paths_summary()
        required = [
            summary["backup"],
            summary["runtime_state"],
        ]
        object_storage_backend = self.settings.object_storage_backend.strip().lower()
        if object_storage_backend == "local":
            required.append(summary["object_storage_local"])
        missing_or_unwritable = [
            item
            for item in required
            if not item.get("path_configured")
            or not item.get("parent_exists")
            or not item.get("writable_target_or_parent")
        ]
        low_free_space = [item for item in required if item.get("free_bytes_sufficient") is False]
        readiness_checks = {
            "required_paths_configured": all(
                bool(item.get("path_configured")) for item in required
            ),
            "required_paths_available_and_writable": not missing_or_unwritable,
            "required_paths_free_space_sufficient": not low_free_space,
        }
        status = "healthy" if not missing_or_unwritable and not low_free_space else "degraded"
        if missing_or_unwritable:
            reason = "one or more required runtime paths are missing or unwritable"
        elif low_free_space:
            reason = "one or more required runtime paths are below the free-space floor"
        else:
            reason = "required runtime paths have writable targets or parent directories"
        return {
            "name": "runtime_paths",
            "status": status,
            "details": {
                "schema_version": "runtime_paths.v1",
                "required_path_count": len(required),
                "unavailable_path_count": len(missing_or_unwritable),
                "low_free_space_path_count": len(low_free_space),
                "min_free_bytes": self.settings.runtime_path_min_free_bytes,
                "paths": summary,
                "reason": reason,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
        }

    def _runtime_paths_summary(self) -> dict:
        return {
            "backup": self._path_readiness(
                "backup",
                self.settings.backup_path,
                required=True,
            ),
            "runtime_state": self._path_readiness(
                "runtime_state",
                self.settings.runtime_state_path,
                required=True,
            ),
            "object_storage_local": self._path_readiness(
                "object_storage_local",
                self.settings.object_storage_local_path,
                required=self.settings.object_storage_backend.strip().lower() == "local",
            ),
        }

    def _database_migrations_check(self) -> dict:
        production = self.settings.env.strip().lower() == "production"
        try:
            database_url = self.settings.resolved_database_url()
            parsed_database_url = make_url(database_url)
            redacted_url = parsed_database_url.render_as_string(hide_password=True)
            alembic_config = Config("alembic.ini")
            script = ScriptDirectory.from_config(alembic_config)
            head_revisions = sorted(script.get_heads())
            sqlite_database_path = self._sqlite_database_path(parsed_database_url)
            if sqlite_database_path is not None and not sqlite_database_path.exists():
                readiness_checks = {
                    "migration_revision_check_available": True,
                    "migration_heads_configured": bool(head_revisions),
                    "database_revision_present": False,
                    "database_schema_at_head": False,
                }
                return {
                    "name": "database_migrations",
                    "status": "degraded" if production else "healthy",
                    "details": {
                        "schema_version": "database_migrations_readiness.v1",
                        "enforced": production,
                        "current_revisions": [],
                        "head_revisions": head_revisions,
                        "database_url": redacted_url,
                        "database_file_exists": False,
                        "readiness_checks": readiness_checks,
                        "failed_readiness_checks": [
                            name for name, ready in readiness_checks.items() if not ready
                        ],
                        "reason": "SQLite database file does not exist",
                    },
                }
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    current_revisions = sorted(
                        MigrationContext.configure(connection).get_current_heads()
                    )
            finally:
                engine.dispose()
        except (RuntimeError, SQLAlchemyError, OSError, ValueError) as exc:
            readiness_checks = {
                "migration_revision_check_available": False,
                "migration_heads_configured": False,
                "database_revision_present": False,
                "database_schema_at_head": False,
            }
            return {
                "name": "database_migrations",
                "status": "degraded" if production else "healthy",
                "details": {
                    "schema_version": "database_migrations_readiness.v1",
                    "enforced": production,
                    "current_revisions": [],
                    "head_revisions": [],
                    "database_url": "",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "readiness_checks": readiness_checks,
                    "failed_readiness_checks": [
                        name for name, ready in readiness_checks.items() if not ready
                    ],
                    "reason": "database migration revision could not be inspected",
                },
            }
        schema_current = bool(head_revisions) and current_revisions == head_revisions
        readiness_checks = {
            "migration_revision_check_available": True,
            "migration_heads_configured": bool(head_revisions),
            "database_revision_present": bool(current_revisions),
            "database_schema_at_head": schema_current,
        }
        if schema_current:
            reason = "database migration revision matches Alembic head"
        elif current_revisions:
            reason = "database migration revision does not match Alembic head"
        else:
            reason = "database has no Alembic revision recorded"
        return {
            "name": "database_migrations",
            "status": "healthy" if schema_current or not production else "degraded",
            "details": {
                "schema_version": "database_migrations_readiness.v1",
                "enforced": production,
                "current_revisions": current_revisions,
                "head_revisions": head_revisions,
                "database_url": redacted_url,
                "database_file_exists": True,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
                "reason": reason,
            },
        }

    def _sqlite_database_path(self, database_url: URL) -> Path | None:
        if not database_url.get_backend_name().startswith("sqlite"):
            return None
        database = database_url.database
        if not database or database == ":memory:":
            return None
        return Path(database).expanduser()

    def _path_readiness(self, name: str, configured_path: str, *, required: bool) -> dict:
        configured = bool(configured_path.strip())
        path = Path(configured_path).expanduser() if configured else Path()
        target = path if path.exists() else path.parent
        target_exists = target.exists()
        target_is_dir = target.exists() and target.is_dir()
        writable = target_is_dir and self._path_writable(target)
        free_bytes = self._path_free_bytes(target) if target_exists else None
        min_free_bytes = self.settings.runtime_path_min_free_bytes
        free_bytes_sufficient = (
            free_bytes is not None and free_bytes >= min_free_bytes
            if required and min_free_bytes > 0
            else True
        )
        return {
            "name": name,
            "path": str(path) if configured else "",
            "required": required,
            "min_free_bytes": min_free_bytes,
            "path_configured": configured,
            "path_exists": path.exists() if configured else False,
            "parent_path": str(path.parent) if configured else "",
            "parent_exists": path.parent.exists() if configured else False,
            "checked_path": str(target) if configured else "",
            "checked_path_exists": target_exists,
            "checked_path_is_dir": target_is_dir,
            "writable_target_or_parent": writable,
            "free_bytes": free_bytes,
            "free_bytes_sufficient": free_bytes_sufficient,
        }

    def _path_writable(self, path: Path) -> bool:
        try:
            return path.exists() and path.is_dir() and os.access(path, os.W_OK)
        except OSError:
            return False

    def _path_free_bytes(self, path: Path) -> int | None:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def _auth_mode_configured(self) -> bool:
        return bool(
            self._api_key_auth_mode_ready()
            or self._trusted_identity_auth_mode_ready()
            or self._provider_session_auth_mode_ready()
        )

    def _initial_admin_path_configured(self) -> bool:
        return bool(
            self._api_key_auth_mode_ready()
            or (
                self._trusted_identity_auth_mode_ready()
                and (
                    self.settings.auth_trusted_default_role.strip().lower() == "admin"
                    or self._group_role_map_assigns_admin(self.settings.auth_trusted_group_role_map)
                )
            )
            or (
                self._provider_session_auth_mode_ready()
                and (
                    self.settings.auth_provider_session_default_role.strip().lower() == "admin"
                    or self._group_role_map_assigns_admin(
                        self.settings.auth_provider_session_group_role_map
                    )
                )
            )
        )

    def _api_key_auth_mode_ready(self) -> bool:
        return bool(
            self._configured_string(self.settings.auth_api_key_reference)
            and self.settings.auth_api_key_header.strip()
            and self.settings.auth_role_header.strip()
            and self.settings.auth_user_header.strip()
        )

    def _trusted_identity_auth_mode_ready(self) -> bool:
        return bool(
            self.settings.auth_trusted_identity_enabled
            and self.settings.auth_trusted_identity_header.strip()
            and self.settings.auth_trusted_email_header.strip()
            and self.settings.auth_trusted_groups_header.strip()
            and self.settings.auth_trusted_default_role.strip().lower() in ROLE_PERMISSIONS
        )

    def _provider_session_auth_mode_ready(self) -> bool:
        provider_client_credentials_status = self._auth_provider_session_client_credentials_status()
        return bool(
            self.settings.auth_provider_session_enabled
            and self._configured_string(self.settings.auth_provider_session_introspection_url)
            and self._auth_provider_session_introspection_url_scheme() == "https"
            and self.settings.auth_provider_session_token_header.strip()
            and self.settings.auth_provider_session_user_claim.strip()
            and self.settings.auth_provider_session_groups_claim.strip()
            and self.settings.auth_provider_session_default_role.strip().lower() in ROLE_PERMISSIONS
            and provider_client_credentials_status["status"] in {"not_configured", "resolved"}
        )

    def _group_role_map_assigns_admin(self, value: str) -> bool:
        for item in value.split(","):
            if "=" not in item:
                continue
            _, role = item.split("=", maxsplit=1)
            if role.strip().lower() == "admin":
                return True
        return False

    def _credential_reference_check(
        self,
        model_endpoints: list[ModelEndpoint],
        voicebox_endpoints: list[VoiceboxEndpoint],
        comfyui_endpoints: list[ComfyUiEndpoint],
        publisher_targets: list[PublisherTarget],
    ) -> dict:
        references = self._credential_reference_entries(
            model_endpoints=model_endpoints,
            voicebox_endpoints=voicebox_endpoints,
            comfyui_endpoints=comfyui_endpoints,
            publisher_targets=publisher_targets,
        )
        checked: list[dict] = []
        by_owner_type: dict[str, int] = {}
        by_scheme: dict[str, int] = {}
        resolved_count = 0
        failed_count = 0
        for reference in references:
            owner_type = reference["owner_type"]
            scheme = reference["scheme"]
            by_owner_type[owner_type] = by_owner_type.get(owner_type, 0) + 1
            by_scheme[scheme] = by_scheme.get(scheme, 0) + 1
            try:
                self.secret_resolver.resolve(
                    reference.get("_credential_reference") or reference["reference"]
                )
            except RuntimeError as exc:
                failed_count += 1
                public_reference = {
                    key: value
                    for key, value in reference.items()
                    if key != "_credential_reference"
                }
                checked.append(
                    public_reference
                    | {
                        "status": "unavailable",
                        "error": type(exc).__name__,
                        "reason": str(exc),
                    }
                )
            else:
                resolved_count += 1
                public_reference = {
                    key: value
                    for key, value in reference.items()
                    if key != "_credential_reference"
                }
                checked.append(public_reference | {"status": "resolved"})
        unsupported_count = by_scheme.get("unsupported", 0)
        invalid_count = by_scheme.get("invalid", 0)
        readiness_checks = {
            "active_credential_references_resolve": failed_count == 0,
            "credential_reference_schemes_supported": (unsupported_count + invalid_count) == 0,
        }
        return {
            "name": "credential_references",
            "status": "degraded" if failed_count else "healthy",
            "details": {
                "schema_version": "credential_reference_readiness.v1",
                "checked_count": len(checked),
                "resolved_count": resolved_count,
                "unavailable_count": failed_count,
                "unsupported_count": unsupported_count,
                "invalid_count": invalid_count,
                "by_owner_type": dict(sorted(by_owner_type.items())),
                "by_scheme": dict(sorted(by_scheme.items())),
                "references": checked,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
                "reason": (
                    "one or more active credential references could not be resolved"
                    if failed_count
                    else "all active credential references resolved"
                    if checked
                    else "no active credential references are configured"
                ),
            },
        }

    def _credential_reference_entries(
        self,
        model_endpoints: list[ModelEndpoint],
        voicebox_endpoints: list[VoiceboxEndpoint],
        comfyui_endpoints: list[ComfyUiEndpoint],
        publisher_targets: list[PublisherTarget],
    ) -> list[dict]:
        entries: list[dict] = []
        self._append_settings_credential_references(entries)
        for endpoint in model_endpoints:
            if endpoint.enabled:
                self._append_credential_reference(
                    entries,
                    owner_type="model_endpoint",
                    owner_id=endpoint.id,
                    field="credential_reference",
                    reference=endpoint.credential_reference,
                )
        for endpoint in voicebox_endpoints:
            if endpoint.enabled:
                self._append_credential_reference(
                    entries,
                    owner_type="voicebox_endpoint",
                    owner_id=endpoint.id,
                    field="credential_reference",
                    reference=endpoint.credential_reference,
                )
        for endpoint in comfyui_endpoints:
            if endpoint.enabled:
                self._append_credential_reference(
                    entries,
                    owner_type="comfyui_endpoint",
                    owner_id=endpoint.id,
                    field="credential_reference",
                    reference=endpoint.credential_reference,
                )
        for target in publisher_targets:
            if not target.enabled:
                continue
            self._append_credential_reference(
                entries,
                owner_type="publisher_target",
                owner_id=target.id,
                field="credential_reference",
                reference=target.credential_reference,
            )
            for field in (
                "oauth_refresh_token_reference",
                "oauth_client_id_reference",
                "oauth_client_secret_reference",
            ):
                value = target.capabilities.get(field)
                self._append_credential_reference(
                    entries,
                    owner_type="publisher_target",
                    owner_id=target.id,
                    field=f"capabilities.{field}",
                    reference=value if isinstance(value, str) else None,
                )
        return entries

    def _append_settings_credential_references(self, entries: list[dict]) -> None:
        if not (self.settings.database_url or "").strip():
            self._append_credential_reference(
                entries,
                owner_type="settings",
                owner_id="database",
                field="database_password_reference",
                reference=self.settings.database_password_reference,
            )
        if self.settings.auth_enabled:
            self._append_credential_reference(
                entries,
                owner_type="settings",
                owner_id="auth",
                field="auth_api_key_reference",
                reference=self.settings.auth_api_key_reference,
            )
        if self.settings.auth_enabled and self.settings.auth_provider_session_enabled:
            self._append_credential_reference(
                entries,
                owner_type="settings",
                owner_id="auth_provider_session",
                field="auth_provider_session_client_id_reference",
                reference=self.settings.auth_provider_session_client_id_reference,
            )
            self._append_credential_reference(
                entries,
                owner_type="settings",
                owner_id="auth_provider_session",
                field="auth_provider_session_client_secret_reference",
                reference=self.settings.auth_provider_session_client_secret_reference,
            )
        if self.settings.object_storage_backend.strip().lower() in {
            "s3",
            "s3-compatible",
            "minio",
        }:
            self._append_credential_reference(
                entries,
                owner_type="settings",
                owner_id="object_storage",
                field="object_storage_access_key_reference",
                reference=self.settings.object_storage_access_key_reference,
            )
            self._append_credential_reference(
                entries,
                owner_type="settings",
                owner_id="object_storage",
                field="object_storage_secret_key_reference",
                reference=self.settings.object_storage_secret_key_reference,
            )

    def _append_credential_reference(
        self,
        entries: list[dict],
        owner_type: str,
        owner_id: str,
        field: str,
        reference: str | None,
    ) -> None:
        if not self._configured_string(reference):
            return
        scheme = credential_reference_scheme(reference)
        entries.append(
            {
                "owner_type": owner_type,
                "owner_id": owner_id,
                "field": field,
                "reference": public_credential_reference(reference),
                "scheme": scheme,
                "_credential_reference": reference,
            }
        )

    def _object_storage_endpoint_tcp_probe(self, endpoint: str) -> dict:
        try:
            parsed = urlparse(endpoint)
            host = parsed.hostname
            port = parsed.port
            if not host:
                raise ValueError("object storage endpoint is missing a host")
            if port is None:
                port = 443 if parsed.scheme == "https" else 80
        except ValueError as exc:
            return {"reachable": False, "error": str(exc)}
        timeout = self.settings.redis_timeout_seconds
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"reachable": True, "host": host, "port": port}
        except OSError as exc:
            return {
                "reachable": False,
                "host": host,
                "port": port,
                "error": type(exc).__name__,
                "message": str(exc),
            }

    def _object_storage_bucket_probe(self) -> dict:
        try:
            access_key = self.secret_resolver.resolve(
                self.settings.object_storage_access_key_reference
            )
            secret_key = self.secret_resolver.resolve(
                self.settings.object_storage_secret_key_reference
            )
        except RuntimeError as exc:
            return {
                "available": False,
                "error": type(exc).__name__,
                "reason": str(exc),
            }
        if bool(access_key) != bool(secret_key):
            return {
                "available": False,
                "reason": "S3 access and secret key references must both resolve",
            }
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            return {
                "available": False,
                "error": type(exc).__name__,
                "reason": "boto3 is required for S3 bucket readiness probing",
            }
        kwargs: dict[str, object] = {
            "endpoint_url": self.settings.object_storage_endpoint,
            "region_name": self.settings.object_storage_region,
            "config": Config(
                s3={
                    "addressing_style": (
                        "path" if self.settings.object_storage_force_path_style else "auto"
                    )
                }
            ),
        }
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        try:
            boto3.client("s3", **kwargs).head_bucket(Bucket=self.settings.object_storage_bucket)
        except Exception as exc:
            return {
                "available": False,
                "error": type(exc).__name__,
                "reason": str(exc),
            }
        return {
            "available": True,
            "bucket": self.settings.object_storage_bucket,
            "probe": "head_bucket",
        }

    def _backup_storage_check(self, audit_events: list[AuditEvent]) -> dict:
        root = Path(self.settings.backup_path).expanduser()
        existing = root if root.exists() else root.parent
        checked_path_exists = existing.exists()
        checked_path_is_dir = checked_path_exists and existing.is_dir()
        writable = checked_path_is_dir and self._path_writable(existing)
        details: dict = {
            "path": str(root),
            "path_exists": root.exists(),
            "parent_exists": root.parent.exists(),
            "checked_path": str(existing),
            "checked_path_exists": checked_path_exists,
            "checked_path_is_dir": checked_path_is_dir,
            "writable_target_or_parent": writable,
            "writable_parent": writable,
            "archive_count": 0,
            "readable_archive_count": 0,
            "restore_validated_archive_count": 0,
            "restore_unvalidated_archive_count": 0,
            "unreadable_archive_count": 0,
            "latest_archive": None,
            "latest_restore_validation": None,
        }
        if not writable:
            details = self._backup_storage_details_with_readiness(details)
            return {
                "name": "backup_storage",
                "status": "degraded",
                "details": details | {"reason": "backup path or parent directory is not writable"},
            }
        archives = sorted(
            root.glob("*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        details["archive_count"] = len(archives)
        if not archives:
            details = self._backup_storage_details_with_readiness(details)
            return {
                "name": "backup_storage",
                "status": "degraded",
                "details": details | {"reason": "no backup archives are available"},
            }
        validation_counts = self._backup_archive_validation_counts(archives, audit_events)
        details.update(validation_counts)
        latest = archives[0]
        latest_details = self._latest_backup_archive_details(latest)
        latest_validation = self._latest_backup_restore_validation(
            latest_details,
            latest.name,
            audit_events,
        )
        status = (
            "healthy"
            if latest_details.get("manifest_readable") and latest_validation.get("validated")
            else "degraded"
        )
        if not latest_details.get("manifest_readable"):
            reason = "latest backup archive manifest could not be read"
        elif latest_validation.get("validated"):
            reason = "latest backup archive manifest is readable and restore validation is current"
        else:
            reason = "latest backup archive has not been dry-run restore validated"
        details = self._backup_storage_details_with_readiness(
            details
            | {
                "latest_archive": latest_details,
                "latest_restore_validation": latest_validation,
            }
        )
        return {
            "name": "backup_storage",
            "status": status,
            "details": details | {"reason": reason},
        }

    def _backup_storage_details_with_readiness(self, details: dict) -> dict:
        latest_archive = details.get("latest_archive")
        latest_validation = details.get("latest_restore_validation")
        archive_count = int(details.get("archive_count") or 0)
        readiness_checks = {
            "backup_path_exists_or_parent_exists": (
                bool(details.get("path_exists")) or bool(details.get("parent_exists"))
            ),
            "backup_path_writable": bool(details.get("writable_parent")),
            "backup_archive_available": archive_count > 0,
            "backup_archives_readable": int(details.get("unreadable_archive_count") or 0) == 0,
            "latest_archive_manifest_readable": (
                isinstance(latest_archive, dict) and latest_archive.get("manifest_readable") is True
            ),
            "latest_restore_validation_current": (
                isinstance(latest_validation, dict) and latest_validation.get("validated") is True
            ),
        }
        return details | {
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }

    def _backup_archive_validation_counts(
        self,
        archives: list[Path],
        audit_events: list[AuditEvent],
    ) -> dict:
        readable_count = 0
        validated_count = 0
        unreadable_count = 0
        for archive_path in archives:
            archive_details = self._latest_backup_archive_details(archive_path)
            if not archive_details.get("manifest_readable"):
                unreadable_count += 1
                continue
            readable_count += 1
            validation = self._latest_backup_restore_validation(
                archive_details,
                archive_path.name,
                audit_events,
            )
            if validation.get("validated") is True:
                validated_count += 1
        return {
            "readable_archive_count": readable_count,
            "restore_validated_archive_count": validated_count,
            "restore_unvalidated_archive_count": max(0, readable_count - validated_count),
            "unreadable_archive_count": unreadable_count,
        }

    def _latest_backup_archive_details(self, path: Path) -> dict:
        stat = path.stat()
        now = datetime.now(UTC)
        modified_at = datetime.fromtimestamp(stat.st_mtime, UTC)
        checksum = self._bounded_backup_archive_checksum(path, stat.st_size)
        details: dict = {
            "filename": path.name,
            "size_bytes": stat.st_size,
            "archive_checksum": checksum.get("archive_checksum"),
            "checksum_status": checksum["checksum_status"],
            "checksum_max_bytes": checksum["checksum_max_bytes"],
            "modified_at": modified_at.isoformat(),
            "age_seconds": max(0.0, (now - modified_at).total_seconds()),
            "manifest_readable": False,
        }
        if checksum.get("checksum_skipped_reason"):
            details["checksum_skipped_reason"] = checksum["checksum_skipped_reason"]
        try:
            manifest = self._read_backup_manifest_for_health(path, stat.st_size)
        except (OSError, tarfile.TarError, KeyError, ValueError, json.JSONDecodeError) as exc:
            return details | {
                "error": type(exc).__name__,
                "message": str(exc),
            }
        return details | {
            "manifest_readable": True,
            "backup_id": manifest.get("backup_id"),
            "schema_version": manifest.get("schema_version"),
            "created_at": manifest.get("created_at"),
            "database_total_records": (manifest.get("database") or {}).get("total_records"),
            "object_storage_file_count": (manifest.get("object_storage") or {}).get("file_count"),
            "runtime_state_file_count": (manifest.get("runtime_state") or {}).get("file_count"),
        }

    def _latest_backup_restore_validation(
        self,
        latest_archive: dict,
        latest_filename: str,
        audit_events: list[AuditEvent],
    ) -> dict:
        backup_id = latest_archive.get("backup_id")
        for event in audit_events:
            if event.event_type != "backup.restore_validated":
                continue
            details = event.details or {}
            if backup_id and details.get("backup_id") != backup_id:
                continue
            archive_path = str(details.get("archive_path") or "")
            if archive_path and Path(archive_path).name != latest_filename:
                continue
            expected_checksum = latest_archive.get("archive_checksum")
            audited_checksum = details.get("archive_checksum")
            if not expected_checksum:
                return {
                    "validated": False,
                    "status": "checksum_not_evaluated",
                    "backup_id": backup_id,
                    "reason": (
                        "latest backup archive checksum was not evaluated because "
                        "the archive exceeds the dashboard checksum limit"
                    ),
                    "validated_archive_checksum": audited_checksum,
                }
            if expected_checksum and audited_checksum != expected_checksum:
                return {
                    "validated": False,
                    "status": "checksum_mismatch",
                    "backup_id": backup_id,
                    "reason": (
                        "latest backup archive checksum does not match the "
                        "recorded dry-run restore validation"
                    ),
                    "archive_checksum": expected_checksum,
                    "validated_archive_checksum": audited_checksum,
                }
            restore_plan = details.get("restore_plan") or {}
            summary = restore_plan.get("summary") or {}
            object_storage_validation = self._restore_archive_validation_summary(
                restore_plan.get("object_storage")
            )
            runtime_state_validation = self._restore_archive_validation_summary(
                restore_plan.get("runtime_state")
            )
            validated_at = event.created_at
            if validated_at.tzinfo is None:
                validated_at = validated_at.replace(tzinfo=UTC)
            return {
                "validated": True,
                "status": "validated",
                "backup_id": details.get("backup_id"),
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
                "object_storage_archive_validation": object_storage_validation,
                "runtime_state_archive_validation": runtime_state_validation,
            }
        return {
            "validated": False,
            "status": "missing",
            "backup_id": backup_id,
            "reason": "no backup.restore_validated audit event found for latest archive",
        }

    def _read_backup_manifest_for_health(self, path: Path, size_bytes: int) -> dict:
        try:
            with tarfile.open(path, "r:gz") as archive:
                member = archive.next()
                if member is None or member.name != "manifest.json":
                    raise ValueError("backup archive first member is not manifest.json")
                if not member.isfile():
                    raise ValueError("backup archive manifest.json must be a regular file")
                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError("manifest.json is not readable")
                return json.loads(handle.read().decode("utf-8"))
        except (OSError, tarfile.TarError, KeyError, ValueError, json.JSONDecodeError):
            if size_bytes > BACKUP_HEALTH_CHECKSUM_MAX_BYTES:
                raise
        with tarfile.open(path, "r:gz") as archive:
            member = archive.getmember("manifest.json")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("manifest.json is not readable")
            return json.loads(handle.read().decode("utf-8"))

    def _bounded_backup_archive_checksum(self, path: Path, size_bytes: int) -> dict:
        if size_bytes > BACKUP_HEALTH_CHECKSUM_MAX_BYTES:
            return {
                "archive_checksum": None,
                "checksum_status": "skipped",
                "checksum_skipped_reason": "archive_exceeds_health_checksum_limit",
                "checksum_max_bytes": BACKUP_HEALTH_CHECKSUM_MAX_BYTES,
            }
        return {
            "archive_checksum": self._file_sha256(path),
            "checksum_status": "computed",
            "checksum_max_bytes": BACKUP_HEALTH_CHECKSUM_MAX_BYTES,
        }

    def _restore_archive_validation_summary(self, restore_plan_section: object) -> dict:
        if not isinstance(restore_plan_section, dict):
            return {
                "validated": False,
                "status": "missing",
            }
        archive_validation = restore_plan_section.get("archive_validation")
        if not isinstance(archive_validation, dict):
            return {
                "validated": False,
                "status": "missing",
                "will_restore": bool(restore_plan_section.get("will_restore")),
            }
        return {
            "validated": archive_validation.get("validated") is True,
            "status": "validated" if archive_validation.get("validated") is True else "unvalidated",
            "schema_version": archive_validation.get("schema_version"),
            "will_restore": bool(restore_plan_section.get("will_restore")),
            "expected_count": archive_validation.get("expected_file_count")
            or archive_validation.get("expected_object_count"),
            "archive_count": archive_validation.get("archive_file_count")
            or archive_validation.get("archive_object_count"),
            "total_bytes": archive_validation.get("total_bytes"),
            "size_verified_count": archive_validation.get("size_verified_count"),
            "checksum_verified_count": archive_validation.get("checksum_verified_count"),
        }

    def _file_sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def _runtime_tool_checks(self) -> list[dict]:
        return [
            self._runtime_tool_check("ffmpeg"),
            self._runtime_tool_check("ffprobe"),
        ]

    def _runtime_tool_check(self, tool: str) -> dict:
        path = shutil.which(tool)
        readiness_checks = {"tool_available": bool(path)}
        return {
            "name": tool,
            "status": "healthy" if path else "degraded",
            "details": {
                "path": path,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
                "reason": (
                    f"{tool} is available on PATH" if path else f"{tool} is not available on PATH"
                ),
            },
        }

    def _redis_runtime_check(self) -> dict:
        redis_modes_enabled = (
            self.settings.redis_event_fanout_enabled or self.settings.redis_worker_signal_enabled
        )
        details = {
            "url_configured": bool(self.settings.redis_url),
            "event_fanout_enabled": self.settings.redis_event_fanout_enabled,
            "event_channel": self.settings.redis_event_channel,
            "worker_signal_enabled": self.settings.redis_worker_signal_enabled,
            "worker_signal_stream": self.settings.redis_worker_signal_stream,
            "worker_signal_maxlen": self.settings.redis_worker_signal_maxlen,
            "timeout_seconds": self.settings.redis_timeout_seconds,
        }
        readiness_checks = {
            "redis_modes_enabled": redis_modes_enabled,
            "url_configured": (not redis_modes_enabled) or bool(self.settings.redis_url),
            "event_channel_configured": (
                (not self.settings.redis_event_fanout_enabled)
                or bool(self.settings.redis_event_channel.strip())
            ),
            "worker_signal_stream_configured": (
                (not self.settings.redis_worker_signal_enabled)
                or bool(self.settings.redis_worker_signal_stream.strip())
            ),
            "worker_signal_maxlen_valid": (
                (not self.settings.redis_worker_signal_enabled)
                or self.settings.redis_worker_signal_maxlen > 0
            ),
        }
        if not redis_modes_enabled:
            return {
                "name": "redis",
                "status": "healthy",
                "details": details
                | {
                    "readiness_checks": readiness_checks | {"redis_reachable": True},
                    "failed_readiness_checks": [],
                    "reason": "Redis-backed event fan-out and worker signals are disabled",
                },
            }
        if not self.settings.redis_url:
            return {
                "name": "redis",
                "status": "degraded",
                "details": details
                | {
                    "readiness_checks": readiness_checks | {"redis_reachable": False},
                    "failed_readiness_checks": [
                        name for name, ready in readiness_checks.items() if not ready
                    ]
                    + ["redis_reachable"],
                    "reason": "Redis URL is not configured",
                },
            }
        probe = self._redis_tcp_probe(self.settings.redis_url)
        readiness_checks = readiness_checks | {"redis_reachable": probe["reachable"]}
        redis_ready = all(readiness_checks.values())
        return {
            "name": "redis",
            "status": "healthy" if redis_ready else "degraded",
            "details": details
            | {
                "tcp_probe": probe,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
                "reason": (
                    "Redis is reachable for event fan-out and worker signals"
                    if redis_ready
                    else (
                        "Redis is reachable but runtime configuration has failed readiness checks"
                    )
                    if probe["reachable"]
                    else "Redis is not reachable"
                ),
            },
        }

    def _redis_tcp_probe(self, redis_url: str) -> dict:
        try:
            parsed = urlparse(redis_url)
            host = parsed.hostname
            port = parsed.port or 6379
            if not host:
                raise ValueError("Redis URL is missing a host")
        except ValueError as exc:
            return {"reachable": False, "error": str(exc)}
        timeout = self.settings.redis_timeout_seconds
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"reachable": True, "host": host, "port": port}
        except OSError as exc:
            return {
                "reachable": False,
                "host": host,
                "port": port,
                "error": type(exc).__name__,
                "message": str(exc),
            }

    def _auth_runtime_check(self) -> dict:
        api_key_ready = self.settings.auth_api_key_reference is not None
        api_key_header_configured = bool(self.settings.auth_api_key_header.strip())
        role_header_configured = bool(self.settings.auth_role_header.strip())
        user_header_configured = bool(self.settings.auth_user_header.strip())
        trusted_identity_header_configured = bool(
            self.settings.auth_trusted_identity_header.strip()
        )
        trusted_email_header_configured = bool(self.settings.auth_trusted_email_header.strip())
        trusted_groups_header_configured = bool(self.settings.auth_trusted_groups_header.strip())
        provider_token_header_configured = bool(
            self.settings.auth_provider_session_token_header.strip()
        )
        provider_user_claim_configured = bool(
            self.settings.auth_provider_session_user_claim.strip()
        )
        provider_groups_claim_configured = bool(
            self.settings.auth_provider_session_groups_claim.strip()
        )
        api_key_reference_status = self._auth_api_key_reference_status()
        api_key_reference_resolves = api_key_reference_status["status"] in {
            "disabled",
            "resolved",
        }
        trusted_default_role_valid = (
            self.settings.auth_trusted_default_role.strip().lower() in ROLE_PERMISSIONS
        )
        provider_default_role_valid = (
            self.settings.auth_provider_session_default_role.strip().lower() in ROLE_PERMISSIONS
        )
        trusted_ready = (
            self.settings.auth_trusted_identity_enabled
            and trusted_default_role_valid
            and trusted_identity_header_configured
            and trusted_email_header_configured
            and trusted_groups_header_configured
        )
        provider_introspection_configured = (
            self.settings.auth_provider_session_introspection_url is not None
        )
        provider_introspection_url_scheme = self._auth_provider_session_introspection_url_scheme()
        provider_introspection_url_secure = (
            not self.settings.auth_provider_session_enabled
            or not provider_introspection_configured
            or provider_introspection_url_scheme == "https"
        )
        provider_client_credentials_status = self._auth_provider_session_client_credentials_status()
        provider_client_credentials_ready = provider_client_credentials_status["status"] in {
            "not_configured",
            "resolved",
        }
        provider_ready = (
            self.settings.auth_provider_session_enabled
            and provider_introspection_configured
            and provider_introspection_url_secure
            and provider_default_role_valid
            and provider_client_credentials_ready
            and provider_token_header_configured
            and provider_user_claim_configured
            and provider_groups_claim_configured
        )
        api_key_mode_ready = api_key_ready and api_key_header_configured
        revocations = self._provider_session_revocation_summary()
        decisions = self._provider_session_decision_summary()
        auth_mode_configured = api_key_mode_ready or trusted_ready or provider_ready
        readiness_checks = {
            "auth_disabled_or_mode_configured": (
                not self.settings.auth_enabled or auth_mode_configured
            ),
            "api_key_or_alternate_auth_mode_configured": (
                not self.settings.auth_enabled
                or api_key_mode_ready
                or trusted_ready
                or provider_ready
            ),
            "api_key_header_configured": (
                not self.settings.auth_enabled or not api_key_ready or api_key_header_configured
            ),
            "role_header_configured": not self.settings.auth_enabled or role_header_configured,
            "user_header_configured": not self.settings.auth_enabled or user_header_configured,
            "api_key_reference_resolves": api_key_reference_resolves,
            "trusted_identity_header_configured": (
                not self.settings.auth_trusted_identity_enabled
                or trusted_identity_header_configured
            ),
            "trusted_email_header_configured": (
                not self.settings.auth_trusted_identity_enabled or trusted_email_header_configured
            ),
            "trusted_groups_header_configured": (
                not self.settings.auth_trusted_identity_enabled or trusted_groups_header_configured
            ),
            "trusted_identity_default_role_valid": (
                not self.settings.auth_trusted_identity_enabled or trusted_default_role_valid
            ),
            "provider_session_introspection_configured": (
                not self.settings.auth_provider_session_enabled or provider_introspection_configured
            ),
            "provider_session_token_header_configured": (
                not self.settings.auth_provider_session_enabled or provider_token_header_configured
            ),
            "provider_session_user_claim_configured": (
                not self.settings.auth_provider_session_enabled or provider_user_claim_configured
            ),
            "provider_session_groups_claim_configured": (
                not self.settings.auth_provider_session_enabled or provider_groups_claim_configured
            ),
            "provider_session_introspection_url_secure": (provider_introspection_url_secure),
            "provider_session_default_role_valid": (
                not self.settings.auth_provider_session_enabled or provider_default_role_valid
            ),
            "provider_session_client_credentials_ready": (
                not self.settings.auth_provider_session_enabled or provider_client_credentials_ready
            ),
            "provider_session_revocation_registry_readable": (
                revocations.get("readable") is not False
            ),
            "provider_session_decision_log_readable": (decisions.get("readable") is not False),
        }
        details = {
            "auth_enabled": self.settings.auth_enabled,
            "api_key_reference_configured": api_key_ready,
            "api_key_header_configured": api_key_header_configured,
            "role_header_configured": role_header_configured,
            "user_header_configured": user_header_configured,
            "api_key_reference_status": api_key_reference_status,
            "trusted_identity_enabled": self.settings.auth_trusted_identity_enabled,
            "trusted_identity_header_configured": trusted_identity_header_configured,
            "trusted_email_header_configured": trusted_email_header_configured,
            "trusted_groups_header_configured": trusted_groups_header_configured,
            "trusted_identity_default_role": self.settings.auth_trusted_default_role,
            "trusted_identity_default_role_valid": trusted_default_role_valid,
            "trusted_identity_group_role_map_configured": bool(
                self.settings.auth_trusted_group_role_map.strip()
            ),
            "provider_session_enabled": self.settings.auth_provider_session_enabled,
            "provider_session_token_header_configured": (provider_token_header_configured),
            "provider_session_user_claim_configured": provider_user_claim_configured,
            "provider_session_groups_claim_configured": provider_groups_claim_configured,
            "provider_session_introspection_configured": provider_introspection_configured,
            "provider_session_introspection_url_scheme": (provider_introspection_url_scheme),
            "provider_session_default_role": self.settings.auth_provider_session_default_role,
            "provider_session_default_role_valid": provider_default_role_valid,
            "provider_session_client_credentials_status": (provider_client_credentials_status),
            "provider_session_group_role_map_configured": bool(
                self.settings.auth_provider_session_group_role_map.strip()
            ),
            "provider_session_decision_log_limit": (
                self.settings.auth_provider_session_decision_log_limit
            ),
            "provider_session_revocations": revocations,
            "provider_session_decisions": decisions,
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }
        issues: list[str] = []
        if not self.settings.auth_enabled:
            return {
                "name": "auth_runtime",
                "status": "healthy",
                "details": details | {"reason": "authentication is disabled"},
            }

        if (
            self.settings.auth_trusted_identity_enabled
            and self.settings.auth_trusted_default_role.strip().lower() not in ROLE_PERMISSIONS
        ):
            issues.append("trusted identity default role is unknown")
        if self.settings.auth_enabled and api_key_ready and not api_key_header_configured:
            issues.append("API-key auth header name is not configured")
        if self.settings.auth_enabled and not role_header_configured:
            issues.append("auth role header name is not configured")
        if self.settings.auth_enabled and not user_header_configured:
            issues.append("auth user header name is not configured")
        if self.settings.auth_trusted_identity_enabled and not trusted_identity_header_configured:
            issues.append("trusted identity header name is not configured")
        if self.settings.auth_trusted_identity_enabled and not trusted_email_header_configured:
            issues.append("trusted email header name is not configured")
        if self.settings.auth_trusted_identity_enabled and not trusted_groups_header_configured:
            issues.append("trusted groups header name is not configured")
        if (
            self.settings.auth_provider_session_enabled
            and self.settings.auth_provider_session_default_role.strip().lower()
            not in ROLE_PERMISSIONS
        ):
            issues.append("provider session default role is unknown")
        if self.settings.auth_provider_session_enabled and not provider_token_header_configured:
            issues.append("provider session token header name is not configured")
        if self.settings.auth_provider_session_enabled and not provider_user_claim_configured:
            issues.append("provider session user claim name is not configured")
        if self.settings.auth_provider_session_enabled and not provider_groups_claim_configured:
            issues.append("provider session groups claim name is not configured")
        if self.settings.auth_provider_session_enabled and not (
            self.settings.auth_provider_session_introspection_url
        ):
            issues.append("provider sessions require an introspection URL")
        if (
            self.settings.auth_provider_session_enabled
            and provider_introspection_configured
            and not provider_introspection_url_secure
        ):
            issues.append("provider session introspection URL must use HTTPS")
        if self.settings.auth_provider_session_enabled and not provider_client_credentials_ready:
            issues.append("provider session client credentials are incomplete")
        if not api_key_reference_resolves:
            issues.append("configured API-key reference is unavailable")

        if not (api_key_mode_ready or trusted_ready or provider_ready):
            issues.append("authentication is enabled but no viable auth mode is configured")

        if revocations.get("readable") is False:
            issues.append("provider session revocation registry is unreadable")
        if decisions.get("readable") is False:
            issues.append("provider session decision log is unreadable")

        return {
            "name": "auth_runtime",
            "status": "degraded" if issues else "healthy",
            "details": details
            | {
                "reason": (
                    "; ".join(issues)
                    if issues
                    else "configured authentication modes are operator-ready"
                ),
            },
        }

    def _auth_api_key_reference_status(self) -> dict:
        reference = self.settings.auth_api_key_reference
        if not self.settings.auth_enabled or not self._configured_string(reference):
            return {"status": "disabled"}
        try:
            self.secret_resolver.resolve(reference)
        except RuntimeError as exc:
            return {
                "status": "unavailable",
                "reference": public_credential_reference(reference),
                "error": type(exc).__name__,
                "reason": str(exc),
            }
        return {"status": "resolved", "reference": public_credential_reference(reference)}

    def _auth_provider_session_introspection_url_scheme(self) -> str | None:
        url = self.settings.auth_provider_session_introspection_url
        if not self._configured_string(url):
            return None
        try:
            return urlparse(str(url).strip()).scheme.lower() or None
        except ValueError:
            return None

    def _auth_provider_session_client_credentials_status(self) -> dict:
        client_id_reference = self.settings.auth_provider_session_client_id_reference
        client_secret_reference = self.settings.auth_provider_session_client_secret_reference
        client_id_reference_configured = self._configured_string(client_id_reference)
        client_secret_reference_configured = self._configured_string(client_secret_reference)
        if not self.settings.auth_enabled or not self.settings.auth_provider_session_enabled:
            return {"status": "disabled"}
        if not client_id_reference_configured and not client_secret_reference_configured:
            return {"status": "not_configured"}
        configured_references = [
            public_credential_reference(reference)
            for reference in (client_id_reference, client_secret_reference)
            if self._configured_string(reference)
        ]
        if client_id_reference_configured != client_secret_reference_configured:
            return {
                "status": "mismatched",
                "client_id_reference_configured": client_id_reference_configured,
                "client_secret_reference_configured": client_secret_reference_configured,
                "configured_references": configured_references,
                "reason": (
                    "provider session client ID and secret references must both be configured"
                ),
            }
        try:
            self.secret_resolver.resolve(client_id_reference)
            self.secret_resolver.resolve(client_secret_reference)
        except RuntimeError as exc:
            return {
                "status": "unavailable",
                "client_id_reference_configured": client_id_reference_configured,
                "client_secret_reference_configured": client_secret_reference_configured,
                "configured_references": configured_references,
                "error": type(exc).__name__,
                "reason": str(exc),
            }
        return {
            "status": "resolved",
            "client_id_reference_configured": True,
            "client_secret_reference_configured": True,
            "configured_references": configured_references,
        }

    def _provider_session_revocation_summary(self) -> dict:
        path = self._provider_session_revocation_path()
        summary = {
            "path": str(path),
            "path_configured": self.settings.auth_provider_session_revocation_path is not None,
            "exists": path.exists(),
            "readable": True,
            "active_count": 0,
            "expired_count": 0,
            "total_count": 0,
        }
        if not path.exists():
            return summary
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return summary | {
                "readable": False,
                "error": type(exc).__name__,
                "reason": "revocation registry could not be read",
            }
        records = payload.get("revocations", [])
        if not isinstance(records, list):
            return summary | {
                "readable": False,
                "error": "invalid_schema",
                "reason": "revocation registry does not contain a revocations list",
            }
        now = datetime.now(UTC)
        revocations = [record for record in records if isinstance(record, dict)]
        expired_count = sum(
            1
            for revocation in revocations
            if self._provider_session_record_expired(revocation, now)
        )
        return summary | {
            "total_count": len(revocations),
            "active_count": len(revocations) - expired_count,
            "expired_count": expired_count,
        }

    def _provider_session_decision_summary(self) -> dict:
        path = self._provider_session_decision_log_path()
        summary = {
            "path": str(path),
            "path_configured": self.settings.auth_provider_session_decision_log_path is not None,
            "exists": path.exists(),
            "readable": True,
            "retention_limit": self.settings.auth_provider_session_decision_log_limit,
            "retained_count": 0,
            "accepted_count": 0,
            "denied_count": 0,
            "error_count": 0,
        }
        if not path.exists():
            return summary
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return summary | {
                "readable": False,
                "error": type(exc).__name__,
                "reason": "decision log could not be read",
            }
        records = payload.get("decisions", [])
        if not isinstance(records, list):
            return summary | {
                "readable": False,
                "error": "invalid_schema",
                "reason": "decision log does not contain a decisions list",
            }
        decisions = [record for record in records if isinstance(record, dict)]
        return summary | {
            "retained_count": len(decisions),
            "accepted_count": sum(
                1 for decision in decisions if decision.get("status") == "accepted"
            ),
            "denied_count": sum(1 for decision in decisions if decision.get("status") == "denied"),
            "error_count": sum(1 for decision in decisions if decision.get("status") == "error"),
        }

    def _provider_session_revocation_path(self) -> Path:
        configured = self.settings.auth_provider_session_revocation_path
        if configured:
            return Path(configured).expanduser()
        return (
            Path(self.settings.runtime_state_path).expanduser()
            / "auth"
            / "provider-session-revocations.json"
        )

    def _provider_session_decision_log_path(self) -> Path:
        configured = self.settings.auth_provider_session_decision_log_path
        if configured:
            return Path(configured).expanduser()
        return (
            Path(self.settings.runtime_state_path).expanduser()
            / "auth"
            / "provider-session-decisions.json"
        )

    def _provider_session_record_expired(self, record: dict, now: datetime) -> bool:
        expires_at = record.get("expires_at")
        if not isinstance(expires_at, str) or not expires_at:
            return False
        try:
            return datetime.fromisoformat(expires_at) <= now
        except ValueError:
            return False

    def _worker_signal_check(self, summary: dict) -> dict:
        failed_count = int(summary.get("failed_count") or 0)
        blocking_count = int(summary.get("blocking_count") or 0)
        malformed_count = int(summary.get("malformed_count") or 0)
        readiness_checks = {
            "worker_signal_summary_supplied": True,
            "worker_signal_delivery_not_failed": failed_count == 0,
            "worker_signals_not_blocking": blocking_count == 0,
        }
        return {
            "name": "worker_signals",
            "status": "degraded" if failed_count > 0 or blocking_count > 0 else "healthy",
            "details": {
                "schema_version": summary.get("schema_version"),
                "recent_count": int(summary.get("recent_count") or 0),
                "blocking_count": blocking_count,
                "failed_count": failed_count,
                "malformed_count": malformed_count,
                "attention_count": failed_count + blocking_count,
                "by_status": summary.get("by_status") or {},
                "by_signal_type": summary.get("by_signal_type") or {},
                "by_target_role": summary.get("by_target_role") or {},
                "active_blocking_target_roles": summary.get("active_blocking_target_roles") or [],
                "by_active_blocking_target_role": summary.get("by_active_blocking_target_role")
                or {},
                "by_delivery_source": summary.get("by_delivery_source") or {},
                "latest_signal": summary.get("latest_signal"),
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, ready in readiness_checks.items() if not ready
                ],
            },
        }

    def _temporal_runtime_check(self, worker_status: WorkerStatusSummary | None = None) -> dict:
        mode = self.settings.temporal_backend_mode.strip().lower() or "local"
        temporal_worker_active = self._temporal_worker_active(worker_status)
        temporal_worker_execution = self._temporal_worker_execution_evidence(worker_status)
        probe: dict | None = None
        details = {
            "mode": mode,
            "namespace": self.settings.temporal_namespace,
            "task_queue": self.settings.temporal_task_queue,
            "signal_transport_enabled": self.settings.temporal_signal_transport_enabled,
            "signal_endpoint_configured": self._configured_string(
                self.settings.temporal_signal_endpoint
            ),
            "backend_address": self.settings.temporal_backend_address,
            "backend_address_configured": self._configured_string(
                self.settings.temporal_backend_address
            ),
            "tls_enabled": self.settings.temporal_backend_tls_enabled,
            "native_worker_enabled": self.settings.temporal_backend_worker_enabled,
            "temporal_worker_active": temporal_worker_active,
            "temporal_worker_execution": temporal_worker_execution,
            "connect_timeout_seconds": (self.settings.temporal_backend_connect_timeout_seconds),
        }
        if mode not in {"local", "bridge", "external"}:
            details = self._temporal_runtime_details_with_readiness(
                details,
                mode=mode,
                temporal_worker_active=temporal_worker_active,
                temporal_worker_execution=temporal_worker_execution,
                probe=probe,
            )
            return {
                "name": "temporal_runtime",
                "status": "unhealthy",
                "details": details
                | {
                    "reason": (
                        "DIALECTICORE_TEMPORAL_BACKEND_MODE must be local, bridge, or external"
                    ),
                },
            }
        if mode == "local":
            details = self._temporal_runtime_details_with_readiness(
                details,
                mode=mode,
                temporal_worker_active=temporal_worker_active,
                temporal_worker_execution=temporal_worker_execution,
                probe=probe,
            )
            return {
                "name": "temporal_runtime",
                "status": "healthy",
                "details": details
                | {
                    "execution_policy": "local_durable_workflow_control",
                    "reason": "using local workflow state, replay journal, and stage pollers",
                },
            }
        if mode == "bridge":
            missing = []
            if not self.settings.temporal_signal_transport_enabled:
                missing.append("DIALECTICORE_TEMPORAL_SIGNAL_TRANSPORT_ENABLED")
            if not self._configured_string(self.settings.temporal_signal_endpoint):
                missing.append("DIALECTICORE_TEMPORAL_SIGNAL_ENDPOINT")
            details = self._temporal_runtime_details_with_readiness(
                details,
                mode=mode,
                temporal_worker_active=temporal_worker_active,
                temporal_worker_execution=temporal_worker_execution,
                probe=probe,
            )
            return {
                "name": "temporal_runtime",
                "status": "degraded" if missing else "healthy",
                "details": details
                | {
                    "execution_policy": "local_control_with_external_signal_bridge",
                    "missing": missing,
                    "reason": (
                        "bridge mode needs outbound signal transport"
                        if missing
                        else "local workflow state is mirrored to the configured bridge"
                    ),
                },
            }

        missing = []
        if not self._configured_string(self.settings.temporal_backend_address):
            missing.append("DIALECTICORE_TEMPORAL_BACKEND_ADDRESS")
        if not self._configured_string(self.settings.temporal_task_queue):
            missing.append("DIALECTICORE_TEMPORAL_TASK_QUEUE")
        if missing:
            details = self._temporal_runtime_details_with_readiness(
                details,
                mode=mode,
                temporal_worker_active=temporal_worker_active,
                temporal_worker_execution=temporal_worker_execution,
                probe=probe,
            )
            return {
                "name": "temporal_runtime",
                "status": "degraded",
                "details": details
                | {
                    "execution_policy": "external_temporal_backend_requested",
                    "missing": missing,
                    "reason": (
                        "external mode is selected but required Temporal settings are missing"
                    ),
                },
            }

        probe = self._temporal_backend_tcp_probe(str(self.settings.temporal_backend_address))
        details = self._temporal_runtime_details_with_readiness(
            details,
            mode=mode,
            temporal_worker_active=temporal_worker_active,
            temporal_worker_execution=temporal_worker_execution,
            probe=probe,
        )
        worker_ready = self.settings.temporal_backend_worker_enabled
        heartbeat_ready = temporal_worker_active is not False
        execution_ready = temporal_worker_execution["status"] == "running"
        status = (
            "healthy"
            if probe["reachable"] and worker_ready and heartbeat_ready and execution_ready
            else "degraded"
        )
        reason = "native Temporal backend address is reachable and worker mode is enabled"
        if not probe["reachable"]:
            reason = "native Temporal backend address is not reachable"
        elif not worker_ready:
            reason = (
                "native Temporal backend is reachable, but "
                "DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED is false"
            )
        elif temporal_worker_active is False:
            reason = "native Temporal backend is reachable, but temporal-worker is not active"
        elif not execution_ready:
            reason = temporal_worker_execution["reason"]
        return {
            "name": "temporal_runtime",
            "status": status,
            "details": details
            | {
                "execution_policy": "external_temporal_backend_requested",
                "tcp_probe": probe,
                "reason": reason,
            },
        }

    def _temporal_runtime_details_with_readiness(
        self,
        details: dict,
        *,
        mode: str,
        temporal_worker_active: bool | None,
        temporal_worker_execution: dict,
        probe: dict | None,
    ) -> dict:
        external = mode == "external"
        bridge = mode == "bridge"
        readiness_checks = {
            "temporal_mode_valid": mode in {"local", "bridge", "external"},
            "bridge_signal_transport_configured": (
                not bridge or self.settings.temporal_signal_transport_enabled
            ),
            "bridge_signal_endpoint_configured": (
                not bridge or self._configured_string(self.settings.temporal_signal_endpoint)
            ),
            "external_backend_address_configured": (
                not external or self._configured_string(self.settings.temporal_backend_address)
            ),
            "external_task_queue_configured": (
                not external or self._configured_string(self.settings.temporal_task_queue)
            ),
            "external_backend_reachable": (
                not external or (probe is not None and probe.get("reachable") is True)
            ),
            "external_native_worker_enabled": (
                not external or self.settings.temporal_backend_worker_enabled
            ),
            "external_temporal_worker_active": (
                not external or temporal_worker_active is not False
            ),
            "external_temporal_worker_execution_running": (
                not external or temporal_worker_execution.get("status") == "running"
            ),
        }
        return details | {
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }

    def _temporal_worker_active(self, worker_status: WorkerStatusSummary | None) -> bool | None:
        if worker_status is None:
            return None
        return any(
            worker.role == "temporal-worker"
            and not worker.stale
            and worker.status in {"running", "idle"}
            for worker in worker_status.workers
        )

    def _temporal_worker_execution_evidence(
        self,
        worker_status: WorkerStatusSummary | None,
    ) -> dict:
        if worker_status is None:
            return {
                "status": "unknown",
                "reason": "worker status was not supplied for Temporal execution evidence",
            }
        temporal_workers = [
            worker
            for worker in worker_status.workers
            if worker.role == "temporal-worker" and not worker.stale
        ]
        if not temporal_workers:
            return {
                "status": "missing",
                "reason": "no active temporal-worker heartbeat is available",
            }
        for worker in temporal_workers:
            details = worker.details or {}
            if details.get("schema_version") != "temporal_worker_execution_summary.v1":
                continue
            execution_status = str(details.get("status") or "unknown")
            progressed_stage_count = int(details.get("progressed_stage_count") or 0)
            error_count = int(details.get("error_count") or 0)
            activity_order = details.get("activity_order")
            activity_count = len(activity_order) if isinstance(activity_order, list) else 0
            return {
                "status": execution_status,
                "worker_id": worker.worker_id,
                "heartbeat_age_seconds": worker.heartbeat_age_seconds,
                "progressed_stage_count": progressed_stage_count,
                "error_count": error_count,
                "activity_count": activity_count,
                "reason": details.get("reason")
                or (
                    "external Temporal activity execution evidence is available"
                    if execution_status == "running"
                    else "temporal-worker has not completed an executable activity pass"
                ),
            }
        return {
            "status": "missing",
            "reason": (
                "temporal-worker heartbeat is active, but no "
                "temporal_worker_execution_summary.v1 evidence is present"
            ),
        }

    def _temporal_backend_tcp_probe(self, address: str) -> dict:
        try:
            host, port = self._parse_temporal_backend_address(address)
        except ValueError as exc:
            return {"reachable": False, "error": str(exc)}
        timeout = self.settings.temporal_backend_connect_timeout_seconds
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"reachable": True, "host": host, "port": port}
        except OSError as exc:
            return {
                "reachable": False,
                "host": host,
                "port": port,
                "error": str(exc),
            }

    def _parse_temporal_backend_address(self, address: str) -> tuple[str, int]:
        raw = address.strip()
        if not raw:
            raise ValueError("Temporal backend address is empty")
        parsed = urlparse(raw if "://" in raw else f"temporal://{raw}")
        host = parsed.hostname
        if not host:
            raise ValueError("Temporal backend address is missing a host")
        port = parsed.port
        if port is None:
            port = 7233
        return host, port

    def _endpoint_collection_status(
        self,
        endpoints: list[ModelEndpoint | VoiceboxEndpoint | ComfyUiEndpoint | PublisherTarget],
    ) -> str:
        if not endpoints:
            return "degraded"
        enabled = [endpoint for endpoint in endpoints if endpoint.enabled]
        if not enabled:
            return "degraded"
        unhealthy = [
            endpoint for endpoint in enabled if endpoint.health_status in {"unhealthy", "failed"}
        ]
        if unhealthy:
            return "degraded"
        unknown = [endpoint for endpoint in enabled if endpoint.health_status == "unknown"]
        if unknown:
            return "degraded"
        return "healthy"

    def _endpoint_collection_details(
        self,
        endpoints: list[ModelEndpoint | VoiceboxEndpoint | ComfyUiEndpoint | PublisherTarget],
    ) -> dict:
        configured = len(endpoints)
        enabled = sum(1 for endpoint in endpoints if endpoint.enabled)
        unhealthy = sum(
            1
            for endpoint in endpoints
            if endpoint.enabled and endpoint.health_status in {"unhealthy", "failed"}
        )
        unknown = sum(
            1 for endpoint in endpoints if endpoint.enabled and endpoint.health_status == "unknown"
        )
        readiness_checks = {
            "endpoints_configured": configured > 0,
            "enabled_endpoint_available": enabled > 0,
            "enabled_endpoints_not_unhealthy": unhealthy == 0,
            "enabled_endpoints_health_known": unknown == 0,
        }
        return {
            "configured": configured,
            "enabled": enabled,
            "healthy": sum(
                1
                for endpoint in endpoints
                if endpoint.enabled and endpoint.health_status == "healthy"
            ),
            "unhealthy": unhealthy,
            "unknown": unknown,
            "readiness_checks": readiness_checks,
            "failed_readiness_checks": [
                name for name, ready in readiness_checks.items() if not ready
            ],
        }

    def _counts(
        self,
        episodes: list[Episode],
        projects: list[Project],
        language_profiles: list[LanguageProfile],
        model_endpoints: list[ModelEndpoint],
        participant_profiles: list[ParticipantProfile],
        voicebox_endpoints: list[VoiceboxEndpoint],
        voice_profiles: list[VoiceProfile],
        comfyui_endpoints: list[ComfyUiEndpoint],
        comfyui_workflows: list[ComfyUiWorkflow],
        visual_profiles: list[VisualProfile],
        publisher_targets: list[PublisherTarget],
    ) -> dict:
        publish_jobs = [
            job for episode in episodes for job in episode.publish_jobs if job.status != "replaced"
        ]
        package_manifest_summary = self._publish_package_manifest_summary(episodes)
        return (
            {
                "projects": len(projects),
                "language_profiles": len(language_profiles),
                "episodes": len(episodes),
                "active_episodes": sum(
                    1
                    for episode in episodes
                    if episode.status not in {EpisodeStatus.completed, EpisodeStatus.cancelled}
                ),
                "paused_episodes": sum(
                    1 for episode in episodes if episode.workflow_control.get("paused") is True
                ),
                "cancelled_episodes": sum(
                    1
                    for episode in episodes
                    if episode.status == EpisodeStatus.cancelled
                    or episode.workflow_control.get("cancelled") is True
                ),
                "failed_episodes": sum(
                    1 for episode in episodes if episode.status == EpisodeStatus.failed
                ),
                "awaiting_approval": sum(
                    1
                    for episode in episodes
                    if any(approval.decision == "pending" for approval in episode.approvals)
                ),
                "model_endpoints": len(model_endpoints),
                "participant_profiles": len(participant_profiles),
                "voicebox_endpoints": len(voicebox_endpoints),
                "voice_profiles": len(voice_profiles),
                "comfyui_endpoints": len(comfyui_endpoints),
                "comfyui_workflows": len(comfyui_workflows),
                "visual_profiles": len(visual_profiles),
                "publisher_targets": len(publisher_targets),
                "enabled_publisher_targets": sum(
                    1 for target in publisher_targets if target.enabled
                ),
                "automated_live_publisher_targets": sum(
                    1
                    for target in publisher_targets
                    if target.enabled and target.capabilities.get("automated_live_publish") is True
                ),
                "completed_export_packages": package_manifest_summary["completed_export_packages"],
                "production_manifest_assets": package_manifest_summary[
                    "production_manifest_assets"
                ],
                "invalid_production_manifest_assets": package_manifest_summary[
                    "invalid_production_manifest_assets"
                ],
                "packages_missing_package_qc": package_manifest_summary[
                    "packages_missing_package_qc"
                ],
                "packages_failing_package_qc": package_manifest_summary[
                    "packages_failing_package_qc"
                ],
                "packages_missing_thumbnail": package_manifest_summary[
                    "packages_missing_thumbnail"
                ],
                "packages_missing_subtitles": package_manifest_summary[
                    "packages_missing_subtitles"
                ],
                "packages_missing_production_manifest": package_manifest_summary[
                    "packages_missing_production_manifest"
                ],
            }
            | self._production_run_counts(episodes)
            | self._publish_job_counts(publish_jobs)
            | self._workflow_orchestration_counts(episodes)
            | self._workflow_retry_counts(episodes)
        )

    def _production_run_counts(self, episodes: list[Episode]) -> dict:
        summary = self._production_run_summary(episodes)
        return {
            "production_runs": summary["production_run_count"],
            "active_production_runs": summary["active_production_runs"],
            "running_active_production_runs": summary["running_active_production_runs"],
            "paused_active_production_runs": summary["paused_active_production_runs"],
            "failed_active_production_runs": summary["failed_active_production_runs"],
            "cancelled_active_production_runs": summary["cancelled_active_production_runs"],
            "completion_blocked_production_runs": summary["completion_blocked_production_runs"],
            "production_runs_needing_attention": summary["attention_count"],
        }

    def _publish_job_counts(self, publish_jobs: list[PublishJob]) -> dict:
        return {
            "publish_jobs": len(publish_jobs),
            "submitted_publish_jobs": sum(1 for job in publish_jobs if job.status == "submitted"),
            "completed_publish_jobs": sum(1 for job in publish_jobs if job.status == "completed"),
            "failed_publish_jobs": sum(1 for job in publish_jobs if job.status == "failed"),
            "dry_run_publish_jobs": sum(1 for job in publish_jobs if job.dry_run),
            "live_publish_jobs": sum(1 for job in publish_jobs if not job.dry_run),
        }

    def _production_run_summary(self, episodes: list[Episode]) -> dict:
        entries = [
            entry
            for entry in (self._production_run_entry(episode) for episode in episodes)
            if entry is not None
        ]
        active = [entry for entry in entries if entry["active"]]
        paused = [entry for entry in active if entry["paused"]]
        failed = [entry for entry in active if entry["failed"]]
        cancelled = [entry for entry in active if entry["cancelled"]]
        running = [
            entry
            for entry in active
            if entry["run_state"] == "running"
            and not entry["paused"]
            and not entry["failed"]
            and not entry["cancelled"]
        ]
        by_state: dict[str, int] = {}
        by_stage: dict[str, int] = {}
        for entry in entries:
            state = entry["run_state"] or "untracked"
            stage = entry["current_stage"] or "unknown"
            by_state[state] = by_state.get(state, 0) + 1
            by_stage[stage] = by_stage.get(stage, 0) + 1
        attention_entries = paused + failed + cancelled + running
        completion_blocked = [entry for entry in entries if entry.get("completion_blocked") is True]
        waiting_for_media = [
            entry for entry in entries if entry.get("completion_waiting_for_media") is True
        ]
        waiting_for_action = [
            entry for entry in entries if entry.get("completion_waiting_for_action") is True
        ]
        attention_entries.extend(completion_blocked)
        unique_attention_entries = list(
            {
                str(entry.get("episode_id") or index): entry
                for index, entry in enumerate(attention_entries)
            }.values()
        )
        by_attention_reason: dict[str, int] = {}
        by_completion_failed_check: dict[str, int] = {}
        for entry in unique_attention_entries:
            for reason in entry.get("attention_reasons", []):
                by_attention_reason[reason] = by_attention_reason.get(reason, 0) + 1
            for failed_check in sorted(set(entry.get("completion_failed_checks", []))):
                if not isinstance(failed_check, str) or not failed_check:
                    continue
                by_completion_failed_check[failed_check] = (
                    by_completion_failed_check.get(failed_check, 0) + 1
                )
        latest = max(
            entries,
            key=lambda entry: self._timestamp_sort_key(entry.get("updated_at")),
            default=None,
        )
        attention_count = len(unique_attention_entries)
        return {
            "schema_version": "production_run_summary.v1",
            "episode_count": len(episodes),
            "production_run_count": len(entries),
            "active_production_runs": len(active),
            "running_active_production_runs": len(running),
            "paused_active_production_runs": len(paused),
            "failed_active_production_runs": len(failed),
            "cancelled_active_production_runs": len(cancelled),
            "completion_blocked_production_runs": len(completion_blocked),
            "waiting_for_media_production_runs": len(waiting_for_media),
            "waiting_for_completion_action_production_runs": len(waiting_for_action),
            "attention_count": attention_count,
            "by_state": by_state,
            "by_stage": by_stage,
            "by_attention_reason": dict(sorted(by_attention_reason.items())),
            "by_completion_failed_check": dict(sorted(by_completion_failed_check.items())),
            "attention_runs": [
                self._production_run_attention_entry(entry)
                for entry in unique_attention_entries[:10]
            ],
            "latest_run": latest,
        }

    def _production_run_entry(self, episode: Episode) -> dict | None:
        control = episode.workflow_control or {}
        run = control.get("run")
        has_run = isinstance(run, dict)
        has_control_state = (
            control.get("paused") is True
            or control.get("cancelled") is True
            or episode.status == EpisodeStatus.failed
        )
        if not has_run and not has_control_state:
            return None
        run_data = run if isinstance(run, dict) else {}
        status_value = episode.status.value
        run_state = str(run_data.get("state") or "").strip() or None
        current_stage = str(run_data.get("current_stage") or status_value).strip() or status_value
        paused = control.get("paused") is True
        cancelled = control.get("cancelled") is True or run_state == "cancelled"
        failed = episode.status == EpisodeStatus.failed or run_state == "failed"
        terminal_episode = episode.status in {
            EpisodeStatus.completed,
            EpisodeStatus.cancelled,
        }
        active = not terminal_episode and (
            run_state in {"running", "failed", "cancelled"}
            or paused
            or control.get("cancelled") is True
            or episode.status == EpisodeStatus.failed
        )
        completion_gate = run_data.get("completion_gate")
        completion_gate_status = (
            str(completion_gate.get("status") or "").strip()
            if isinstance(completion_gate, dict)
            else None
        )
        completion_handoff = self._latest_completion_handoff(run_data)
        completion_handoff_status = (
            str(completion_handoff.get("status") or "").strip()
            if isinstance(completion_handoff, dict)
            else None
        )
        completion_handoff_failed_checks = (
            completion_handoff.get("failed_checks", [])
            if isinstance(completion_handoff, dict)
            and isinstance(completion_handoff.get("failed_checks"), list)
            else []
        )
        invalid_completed_gate = (
            current_stage == EpisodeStatus.completed.value and completion_gate_status != "pass"
        )
        completion_stage_blocked = (
            active
            and run_state == "running"
            and completion_handoff_status == "blocked"
        )
        completion_waiting_for_media = (
            completion_stage_blocked and self._episode_has_active_media_jobs(episode)
        )
        completion_waiting_for_action = (
            completion_stage_blocked and not completion_waiting_for_media
        )
        # A blocked completion handoff means ordinary production or review work is
        # still outstanding. It is not an operational failure unless the episode
        # was already marked complete without a passing completion gate.
        completion_blocked = invalid_completed_gate
        attention_reasons: list[str] = []
        if paused and active:
            attention_reasons.append("paused")
        if failed and active:
            attention_reasons.append("failed")
        if cancelled and active:
            attention_reasons.append("cancelled")
        if completion_blocked:
            attention_reasons.append("completion_blocked")
        if completion_waiting_for_media:
            attention_reasons.append("waiting_for_media")
        if completion_waiting_for_action:
            attention_reasons.append("waiting_for_completion_action")
        if active and run_state == "running" and not attention_reasons:
            attention_reasons.append("running")
        updated_at = (
            run_data.get("updated_at")
            or run_data.get("completed_at")
            or run_data.get("started_at")
            or control.get("paused_at")
            or control.get("cancelled_at")
            or episode.updated_at.isoformat()
        )
        return {
            "episode_id": str(episode.id),
            "run_id": run_data.get("run_id"),
            "run_sequence": run_data.get("run_sequence"),
            "run_state": run_state,
            "episode_status": status_value,
            "current_stage": current_stage,
            "active": active,
            "paused": paused,
            "failed": failed,
            "cancelled": cancelled,
            "completion_blocked": completion_blocked,
            "completion_waiting_for_media": completion_waiting_for_media,
            "completion_waiting_for_action": completion_waiting_for_action,
            "completion_gate_status": completion_gate_status,
            "completion_failed_checks": (
                completion_gate.get("failed_checks", [])
                if isinstance(completion_gate, dict)
                and isinstance(completion_gate.get("failed_checks"), list)
                else completion_handoff_failed_checks
            ),
            "completion_handoff_status": completion_handoff_status,
            "attention_reasons": attention_reasons,
            "started_at": run_data.get("started_at"),
            "updated_at": updated_at,
            "stage_history_count": len(run_data.get("stage_history", []))
            if isinstance(run_data.get("stage_history"), list)
            else 0,
            "signal_count": len(run_data.get("signals", []))
            if isinstance(run_data.get("signals"), list)
            else 0,
        }

    @staticmethod
    def _episode_has_active_media_jobs(episode: Episode) -> bool:
        return any(asset.status in {"submitted", "running"} for asset in episode.assets)

    def _latest_completion_handoff(self, run_data: dict) -> dict | None:
        last_worker_orchestration = run_data.get("last_worker_orchestration")
        if not isinstance(last_worker_orchestration, dict):
            return None
        completion_handoff = last_worker_orchestration.get("completion_handoff")
        return completion_handoff if isinstance(completion_handoff, dict) else None

    def _production_run_attention_entry(self, entry: dict) -> dict:
        return {
            "episode_id": entry.get("episode_id"),
            "run_id": entry.get("run_id"),
            "run_sequence": entry.get("run_sequence"),
            "run_state": entry.get("run_state"),
            "episode_status": entry.get("episode_status"),
            "current_stage": entry.get("current_stage"),
            "attention_reasons": entry.get("attention_reasons") or [],
            "completion_gate_status": entry.get("completion_gate_status"),
            "completion_failed_checks": entry.get("completion_failed_checks") or [],
            "completion_handoff_status": entry.get("completion_handoff_status"),
            "updated_at": entry.get("updated_at"),
        }

    def _workflow_duration_observability_summary(self, episodes: list[Episode]) -> dict:
        now = datetime.now(UTC)
        production_duration_ms_sum = 0
        production_duration_record_count = 0
        stage_duration_ms_sum = 0
        stage_duration_record_count = 0
        by_stage: dict[str, dict[str, int]] = {}
        by_language: dict[str, dict[str, int]] = {}

        for episode in episodes:
            control = episode.workflow_control or {}
            run = control.get("run")
            if isinstance(run, dict):
                started_at = self._parse_timestamp(run.get("started_at"))
                ended_at = self._parse_timestamp(run.get("completed_at") or run.get("updated_at"))
                if started_at is not None:
                    end = ended_at or now
                    duration_ms = self._duration_ms_between(started_at, end)
                    if duration_ms is not None:
                        production_duration_record_count += 1
                        production_duration_ms_sum += duration_ms
                stage_entries = self._stage_duration_entries(run, now)
                for stage_entry in stage_entries:
                    duration_ms = stage_entry["duration_ms"]
                    stage_duration_record_count += 1
                    stage_duration_ms_sum += duration_ms
                    stage = stage_entry["stage"]
                    bucket = by_stage.setdefault(
                        stage,
                        {"duration_ms_sum": 0, "duration_record_count": 0},
                    )
                    bucket["duration_ms_sum"] += duration_ms
                    bucket["duration_record_count"] += 1

            for language, duration_ms in self._language_production_durations(episode).items():
                bucket = by_language.setdefault(
                    language,
                    {"duration_ms_sum": 0, "duration_record_count": 0},
                )
                bucket["duration_ms_sum"] += duration_ms
                bucket["duration_record_count"] += 1

        return {
            "production_duration_ms_sum": production_duration_ms_sum,
            "production_duration_record_count": production_duration_record_count,
            "stage_duration_ms_sum": stage_duration_ms_sum,
            "stage_duration_record_count": stage_duration_record_count,
            "by_stage": dict(sorted(by_stage.items())),
            "by_language": dict(sorted(by_language.items())),
        }

    def _workflow_duration_observability_check(self, summary: dict) -> dict:
        readiness_checks = {
            "workflow_run_duration_aggregation_available": True,
            "workflow_stage_duration_aggregation_available": True,
            "language_duration_aggregation_available": True,
        }
        return {
            "name": "workflow_duration_observability",
            "status": "healthy",
            "details": {
                "schema_version": "workflow_duration_observability.v1",
                **summary,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [],
                "reason": (
                    "workflow run, stage, and per-language production durations are aggregated"
                ),
            },
        }

    def _workflow_duration_observability_counts(self, summary: dict) -> dict:
        return {
            "workflow_production_duration_records": int(
                summary["production_duration_record_count"]
            ),
            "workflow_stage_duration_records": int(summary["stage_duration_record_count"]),
            "workflow_language_duration_records": sum(
                int(item.get("duration_record_count") or 0)
                for item in summary["by_language"].values()
            ),
        }

    def _stage_duration_entries(self, run: dict, now: datetime) -> list[dict[str, Any]]:
        raw_history = run.get("stage_history")
        if not isinstance(raw_history, list):
            return []
        history = [
            {
                "stage": str(item.get("stage") or "unknown"),
                "entered_at": self._parse_timestamp(item.get("entered_at")),
            }
            for item in raw_history
            if isinstance(item, dict)
        ]
        history = [item for item in history if item["entered_at"] is not None]
        history.sort(key=lambda item: item["entered_at"])
        run_end = self._parse_timestamp(run.get("completed_at") or run.get("updated_at"))
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(history):
            next_entered_at = history[index + 1]["entered_at"] if index + 1 < len(history) else None
            end = next_entered_at or run_end or now
            duration_ms = self._duration_ms_between(item["entered_at"], end)
            if duration_ms is None:
                continue
            entries.append({"stage": item["stage"], "duration_ms": duration_ms})
        return entries

    def _language_production_durations(self, episode: Episode) -> dict[str, int]:
        timestamps_by_language: dict[str, list[datetime]] = {}
        for asset in episode.assets:
            language = asset.language or "und"
            created_at = self._parse_timestamp(asset.created_at)
            updated_at = self._parse_timestamp(asset.updated_at)
            values = [value for value in (created_at, updated_at) if value is not None]
            if values:
                timestamps_by_language.setdefault(language, []).extend(values)
        durations: dict[str, int] = {}
        for language, timestamps in timestamps_by_language.items():
            start = min(timestamps)
            end = max(timestamps)
            duration_ms = self._duration_ms_between(start, end)
            if duration_ms is not None:
                durations[language] = duration_ms
        return durations

    def _duration_ms_between(self, started_at: datetime, ended_at: datetime) -> int | None:
        if ended_at < started_at:
            return None
        return int((ended_at - started_at).total_seconds() * 1000)

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _publish_job_summary(self, episodes: list[Episode]) -> dict:
        publish_jobs = [
            job for episode in episodes for job in episode.publish_jobs if job.status != "replaced"
        ]
        counts = self._publish_job_counts(publish_jobs)
        manifest_summary = self._publish_package_manifest_summary(episodes)
        latest_failed = max(
            (job for job in publish_jobs if job.status == "failed"),
            key=lambda job: job.updated_at,
            default=None,
        )
        latest_submitted = max(
            (job for job in publish_jobs if job.status == "submitted"),
            key=lambda job: job.updated_at,
            default=None,
        )
        return {
            "schema_version": "publish_job_summary.v1",
            **counts,
            **manifest_summary,
            "latest_failed_job": self._publish_job_entry(latest_failed),
            "latest_submitted_job": self._publish_job_entry(latest_submitted),
            "reason": (
                "publish jobs need operator attention"
                if counts["failed_publish_jobs"]
                or counts["submitted_publish_jobs"]
                or manifest_summary["packages_missing_package_qc"]
                or manifest_summary["packages_failing_package_qc"]
                or manifest_summary["packages_missing_thumbnail"]
                or manifest_summary["packages_missing_subtitles"]
                or manifest_summary["invalid_production_manifest_assets"]
                or manifest_summary["packages_missing_production_manifest"]
                else "publish jobs have no failed or submitted backlog"
            ),
        }

    def _publish_package_manifest_summary(self, episodes: list[Episode]) -> dict:
        completed_packages: list[object] = []
        manifest_package_ids: set[str] = set()
        invalid_entries: list[dict] = []
        missing_entries: list[dict] = []
        missing_package_qc_entries: list[dict] = []
        failing_package_qc_entries: list[dict] = []
        missing_package_thumbnail_entries: list[dict] = []
        missing_package_subtitle_entries: list[dict] = []
        for episode in episodes:
            package_assets_by_id = {
                str(asset.id): asset
                for asset in episode.assets
                if asset.asset_type == AssetType.export_package and asset.status == "completed"
            }
            for asset in episode.assets:
                if (
                    asset.asset_type == AssetType.production_manifest
                    and asset.status == "completed"
                    and asset.source_entity_type == "export_package"
                ):
                    validity = self._production_manifest_asset_valid(
                        asset,
                        package_assets_by_id.get(str(asset.source_entity_id)),
                    )
                    if validity["valid"]:
                        manifest_package_ids.add(asset.source_entity_id)
                    else:
                        invalid_entries.append(
                            {
                                "episode_id": str(episode.id),
                                "manifest_asset_id": str(asset.id),
                                "package_asset_id": asset.source_entity_id,
                                "language": asset.language,
                                "storage_uri_present": bool(asset.storage_uri),
                                "checksum_present": bool(asset.checksum),
                                "created_at": asset.created_at.isoformat(),
                                "reason": validity["reason"],
                            }
                        )
            for asset in episode.assets:
                if asset.asset_type == AssetType.export_package and asset.status == "completed":
                    completed_packages.append(asset)
                    package_qc = self._latest_package_qc(episode, asset)
                    if package_qc is None:
                        missing_package_qc_entries.append(
                            {
                                "episode_id": str(episode.id),
                                "package_asset_id": str(asset.id),
                                "source_entity_type": asset.source_entity_type,
                                "source_entity_id": asset.source_entity_id,
                                "language": asset.language,
                                "storage_uri_present": bool(asset.storage_uri),
                                "checksum_present": bool(asset.checksum),
                                "created_at": asset.created_at.isoformat(),
                            }
                        )
                    elif package_qc.status == "fail" or package_qc.severity == QualitySeverity.fail:
                        failing_package_qc_entries.append(
                            {
                                "episode_id": str(episode.id),
                                "package_asset_id": str(asset.id),
                                "quality_result_id": str(package_qc.id),
                                "quality_result_status": package_qc.status,
                                "quality_result_severity": package_qc.severity.value,
                                "source_entity_type": asset.source_entity_type,
                                "source_entity_id": asset.source_entity_id,
                                "language": asset.language,
                                "storage_uri_present": bool(asset.storage_uri),
                                "checksum_present": bool(asset.checksum),
                                "created_at": asset.created_at.isoformat(),
                                "quality_result_created_at": package_qc.created_at.isoformat(),
                            }
                        )
                    render_asset = self._render_asset_for_package(episode, asset)
                    thumbnail_asset = (
                        self._latest_thumbnail_asset(episode, render_asset)
                        if render_asset is not None
                        else None
                    )
                    subtitle_asset = (
                        self._latest_completed_subtitle_asset_for_render(
                            episode,
                            render_asset,
                        )
                        if render_asset is not None
                        else None
                    )
                    if thumbnail_asset is not None and not self._export_package_includes_thumbnail(
                        asset,
                        thumbnail_asset,
                    ):
                        missing_package_thumbnail_entries.append(
                            self._package_evidence_entry(
                                episode,
                                asset,
                                related_asset=thumbnail_asset,
                                related_asset_key="thumbnail_asset_id",
                            )
                        )
                    if subtitle_asset is not None and not self._export_package_includes_subtitles(
                        asset,
                    ):
                        missing_package_subtitle_entries.append(
                            self._package_evidence_entry(
                                episode,
                                asset,
                                related_asset=subtitle_asset,
                                related_asset_key="subtitle_asset_id",
                            )
                        )
                    if str(asset.id) not in manifest_package_ids:
                        missing_entries.append(
                            {
                                "episode_id": str(episode.id),
                                "package_asset_id": str(asset.id),
                                "source_entity_type": asset.source_entity_type,
                                "source_entity_id": asset.source_entity_id,
                                "language": asset.language,
                                "storage_uri_present": bool(asset.storage_uri),
                                "checksum_present": bool(asset.checksum),
                                "created_at": asset.created_at.isoformat(),
                            }
                        )
        latest_missing = max(
            missing_entries,
            key=lambda entry: str(entry.get("created_at") or ""),
            default=None,
        )
        latest_missing_package_qc = max(
            missing_package_qc_entries,
            key=lambda entry: str(entry.get("created_at") or ""),
            default=None,
        )
        latest_failing_package_qc = max(
            failing_package_qc_entries,
            key=lambda entry: str(entry.get("quality_result_created_at") or ""),
            default=None,
        )
        latest_missing_package_thumbnail = max(
            missing_package_thumbnail_entries,
            key=lambda entry: str(entry.get("created_at") or ""),
            default=None,
        )
        latest_missing_package_subtitles = max(
            missing_package_subtitle_entries,
            key=lambda entry: str(entry.get("created_at") or ""),
            default=None,
        )
        latest_invalid = max(
            invalid_entries,
            key=lambda entry: str(entry.get("created_at") or ""),
            default=None,
        )
        return {
            "completed_export_packages": len(completed_packages),
            "production_manifest_assets": len(manifest_package_ids),
            "invalid_production_manifest_assets": len(invalid_entries),
            "packages_missing_production_manifest": len(missing_entries),
            "packages_missing_package_qc": len(missing_package_qc_entries),
            "packages_failing_package_qc": len(failing_package_qc_entries),
            "packages_missing_thumbnail": len(missing_package_thumbnail_entries),
            "packages_missing_subtitles": len(missing_package_subtitle_entries),
            "latest_package_missing_production_manifest": latest_missing,
            "latest_package_missing_package_qc": latest_missing_package_qc,
            "latest_package_failing_package_qc": latest_failing_package_qc,
            "latest_package_missing_thumbnail": latest_missing_package_thumbnail,
            "latest_package_missing_subtitles": latest_missing_package_subtitles,
            "latest_invalid_production_manifest": latest_invalid,
            "packages_missing_production_manifest_examples": missing_entries[:5],
            "packages_missing_package_qc_examples": missing_package_qc_entries[:5],
            "packages_failing_package_qc_examples": failing_package_qc_entries[:5],
            "packages_missing_thumbnail_examples": missing_package_thumbnail_entries[:5],
            "packages_missing_subtitles_examples": missing_package_subtitle_entries[:5],
            "invalid_production_manifest_examples": invalid_entries[:5],
        }

    def _package_evidence_entry(
        self,
        episode: Episode,
        package_asset: Asset,
        *,
        related_asset: Asset,
        related_asset_key: str,
    ) -> dict:
        return {
            "episode_id": str(episode.id),
            "package_asset_id": str(package_asset.id),
            related_asset_key: str(related_asset.id),
            "source_entity_type": package_asset.source_entity_type,
            "source_entity_id": package_asset.source_entity_id,
            "language": package_asset.language,
            "storage_uri_present": bool(package_asset.storage_uri),
            "checksum_present": bool(package_asset.checksum),
            "created_at": package_asset.created_at.isoformat(),
        }

    def _latest_package_qc(self, episode: Episode, asset: Asset) -> QualityResult | None:
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "youtube_package_integrity"
                and result.target_id == str(asset.id)
            ),
            None,
        )

    def _render_asset_for_package(self, episode: Episode, package_asset: Asset) -> Asset | None:
        if package_asset.source_entity_type != "render_asset":
            return None
        return next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.render
                and str(asset.id) == str(package_asset.source_entity_id)
            ),
            None,
        )

    def _latest_thumbnail_asset(self, episode: Episode, render_asset: Asset) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.thumbnail
                and asset.status == "completed"
                and asset.source_entity_type == "render_asset"
                and asset.source_entity_id == str(render_asset.id)
            ),
            None,
        )

    def _latest_completed_subtitle_asset_for_render(
        self,
        episode: Episode,
        render_asset: Asset,
    ) -> Asset | None:
        transcript_id = self._transcript_id_for_render(episode, render_asset)
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.subtitle
                and asset.status == "completed"
                and asset.language == render_asset.language
                and (
                    transcript_id is None
                    or (
                        asset.source_entity_type == "transcript_version"
                        and asset.source_entity_id == transcript_id
                    )
                )
            ),
            None,
        )

    def _transcript_id_for_render(self, episode: Episode, render_asset: Asset) -> str | None:
        timeline_id = render_asset.generation_metadata.get("timeline_asset_id")
        if not timeline_id and render_asset.source_entity_type == "timeline_asset":
            timeline_id = render_asset.source_entity_id
        if not timeline_id:
            return None
        timeline_asset = next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.timeline and str(asset.id) == str(timeline_id)
            ),
            None,
        )
        if timeline_asset is None:
            return None
        transcript_id = timeline_asset.generation_metadata.get("transcript_version_id")
        if isinstance(transcript_id, str) and transcript_id:
            return transcript_id
        timeline = timeline_asset.generation_metadata.get("timeline_json")
        if isinstance(timeline, dict) and isinstance(timeline.get("transcript_version_id"), str):
            return timeline["transcript_version_id"]
        return None

    def _export_package_includes_thumbnail(
        self,
        package_asset: Asset,
        thumbnail_asset: Asset,
    ) -> bool:
        metadata = package_asset.generation_metadata
        package_thumbnail_id = metadata.get("thumbnail_asset_id")
        if package_thumbnail_id is not None and str(package_thumbnail_id) != str(
            thumbnail_asset.id
        ):
            return False
        manifest = metadata.get("youtube_package_manifest")
        if isinstance(manifest, dict):
            manifest_thumbnail_id = manifest.get("thumbnail_asset_id")
            if manifest_thumbnail_id is not None and str(manifest_thumbnail_id) != str(
                thumbnail_asset.id
            ):
                return False
        included_files = metadata.get("included_files")
        if isinstance(included_files, list):
            return "thumbnail/thumbnail.jpg" in included_files
        return package_thumbnail_id is not None or (
            isinstance(manifest, dict) and manifest.get("thumbnail_asset_id") is not None
        )

    def _export_package_includes_subtitles(self, package_asset: Asset) -> bool:
        metadata = package_asset.generation_metadata
        included_files = metadata.get("included_files")
        if isinstance(included_files, list):
            return any(
                isinstance(name, str) and name.startswith("subtitles/")
                for name in included_files
            )
        manifest = metadata.get("youtube_package_manifest")
        if isinstance(manifest, dict):
            subtitles = manifest.get("subtitles")
            return isinstance(subtitles, list) and bool(subtitles)
        return False

    def _production_manifest_asset_valid(
        self,
        asset: Asset,
        package_asset: Asset | None = None,
    ) -> dict[str, object]:
        manifest = asset.generation_metadata.get("production_manifest")
        if not isinstance(manifest, dict):
            return {"valid": False, "reason": "embedded production_manifest is missing"}
        if manifest.get("schema_version") != "production_manifest.v1":
            return {
                "valid": False,
                "reason": "embedded production_manifest schema_version is invalid",
            }
        delivery_package = manifest.get("delivery_package")
        if not isinstance(delivery_package, dict) or not delivery_package.get("asset_id"):
            return {
                "valid": False,
                "reason": "embedded delivery package asset_id is missing",
            }
        embedded_package_id = delivery_package.get("asset_id")
        if str(embedded_package_id) != str(asset.source_entity_id):
            return {
                "valid": False,
                "reason": "embedded delivery package asset_id does not match source_entity_id",
            }
        if package_asset is not None:
            embedded_checksum = delivery_package.get("checksum")
            if (
                embedded_checksum
                and package_asset.checksum
                and str(embedded_checksum) != str(package_asset.checksum)
            ):
                return {
                    "valid": False,
                    "reason": "embedded delivery package checksum does not match package asset",
                }
            embedded_storage_uri = delivery_package.get("storage_uri")
            if (
                embedded_storage_uri
                and package_asset.storage_uri
                and str(embedded_storage_uri) != str(package_asset.storage_uri)
            ):
                return {
                    "valid": False,
                    "reason": "embedded delivery package storage_uri does not match package asset",
                }
            embedded_delivery_package_id = delivery_package.get("package_id")
            current_package_id = package_asset.generation_metadata.get("package_id")
            if (
                embedded_delivery_package_id
                and current_package_id
                and str(embedded_delivery_package_id) != str(current_package_id)
            ):
                return {
                    "valid": False,
                    "reason": "embedded delivery package package_id does not match package asset",
                }
        talkshow_visual_validity = self._production_manifest_talkshow_visuals_valid(manifest)
        if talkshow_visual_validity["valid"] is False:
            return talkshow_visual_validity
        chapter_validity = self._production_manifest_chapters_valid(
            manifest,
            delivery_package,
        )
        if chapter_validity["valid"] is False:
            return chapter_validity
        return {"valid": True, "reason": "production manifest is structurally valid"}

    def _production_manifest_chapters_valid(self, manifest: dict, delivery_package: dict) -> dict:
        timeline = manifest.get("timeline")
        if not isinstance(timeline, dict):
            return {"valid": True, "reason": None}
        expected_chapters = timeline.get("chapters")
        expected_count = int(timeline.get("chapter_count") or 0)
        if expected_count <= 0 and not expected_chapters:
            return {"valid": True, "reason": None}
        if not isinstance(expected_chapters, list) or len(expected_chapters) < expected_count:
            return {
                "valid": False,
                "reason": "embedded production manifest timeline chapters are missing",
            }
        package_manifest = delivery_package.get("manifest")
        if not isinstance(package_manifest, dict):
            return {
                "valid": False,
                "reason": "embedded delivery package manifest is missing",
            }
        package_chapters = package_manifest.get("chapters")
        if not self._chapter_entries_match(expected_chapters, package_chapters):
            return {
                "valid": False,
                "reason": "embedded delivery package chapters do not match timeline chapters",
            }
        return {"valid": True, "reason": None}

    def _chapter_entries_match(self, expected: list, actual: object) -> bool:
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        actual_by_start = {
            int(chapter.get("start_ms") or 0): chapter
            for chapter in actual
            if isinstance(chapter, dict)
        }
        for chapter in expected:
            if not isinstance(chapter, dict):
                continue
            start_ms = int(chapter.get("start_ms") or 0)
            actual_chapter = actual_by_start.get(start_ms)
            if actual_chapter is None:
                return False
            if str(actual_chapter.get("title") or "") != str(chapter.get("title") or ""):
                return False
        return True

    def _production_manifest_talkshow_visuals_valid(self, manifest: dict) -> dict[str, object]:
        talkshow_visuals = manifest.get("talkshow_visuals")
        if not isinstance(talkshow_visuals, dict):
            if self._production_manifest_has_reusable_visual_segments(manifest):
                return {
                    "valid": False,
                    "reason": "embedded talkshow visual handoff is missing",
                }
            return {"valid": True, "reason": None}
        if talkshow_visuals.get("schema_version") != "talkshow_visual_handoff.v1":
            return {
                "valid": False,
                "reason": "embedded talkshow visual handoff schema_version is invalid",
            }
        for role, label in (
            ("reaction_loop", "reaction-loop"),
            ("studio_scene", "studio-scene"),
        ):
            section = talkshow_visuals.get(role)
            if not isinstance(section, dict):
                return {
                    "valid": False,
                    "reason": f"embedded talkshow visual handoff {label} section is missing",
                }
            expected_count = int(section.get("expected_segment_count") or 0)
            linked_count = int(section.get("linked_segment_count") or 0)
            missing_ids = section.get("missing_segment_ids")
            if (
                expected_count > linked_count
                or (isinstance(missing_ids, list) and missing_ids)
                or section.get("ready") is False
            ):
                return {
                    "valid": False,
                    "reason": f"embedded talkshow visual handoff has missing {label} segments",
                }
        if talkshow_visuals.get("ready") is False:
            return {
                "valid": False,
                "reason": "embedded talkshow visual handoff is not ready",
            }
        return {"valid": True, "reason": None}

    def _production_manifest_has_reusable_visual_segments(self, manifest: dict) -> bool:
        for segment in manifest.get("timeline_segments", []):
            if not isinstance(segment, dict):
                continue
            if segment.get("reaction_visual_asset_id") or segment.get("studio_scene_asset_id"):
                return True
            for layer in segment.get("visual_layers", []):
                if isinstance(layer, dict) and layer.get("role") in {
                    "reaction_loop",
                    "studio_scene",
                }:
                    return True
        return False

    def _publish_job_entry(self, job: PublishJob | None) -> dict | None:
        if job is None:
            return None
        return {
            "job_id": str(job.id),
            "episode_id": str(job.episode_id),
            "publisher_target_id": job.publisher_target_id,
            "platform": job.platform,
            "package_asset_id": str(job.package_asset_id),
            "status": job.status,
            "dry_run": job.dry_run,
            "requested_at": job.requested_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }

    def _workflow_orchestration_counts(self, episodes: list[Episode]) -> dict:
        summary = self._workflow_orchestration_summary(episodes)
        return {
            "workflow_orchestration_attempts": summary["attempt_count"],
            "workflow_orchestration_errors": summary["error_count"],
            "temporal_stage_dispatches": summary["dispatch_count"],
            "blocked_temporal_stage_dispatches": summary["blocked_dispatch_count"],
        }

    def _workflow_orchestration_summary(self, episodes: list[Episode]) -> dict:
        attempts = self._workflow_orchestration_entries(episodes)
        dispatches = self._temporal_dispatch_entries(episodes)
        by_worker: dict[str, int] = {}
        by_policy: dict[str, int] = {}
        for attempt in attempts:
            by_worker[attempt["worker_id"]] = by_worker.get(attempt["worker_id"], 0) + 1
            by_policy[attempt["policy"]] = by_policy.get(attempt["policy"], 0) + 1
        by_dispatch_status: dict[str, int] = {}
        for dispatch in dispatches:
            by_dispatch_status[dispatch["status"]] = (
                by_dispatch_status.get(dispatch["status"], 0) + 1
            )
        by_failed_stage: dict[str, int] = {}
        by_progressed_stage: dict[str, int] = {}
        for attempt in attempts:
            for stage, count in attempt.get("by_failed_stage", {}).items():
                by_failed_stage[stage] = by_failed_stage.get(stage, 0) + int(count)
            for stage, count in attempt.get("by_progressed_stage", {}).items():
                by_progressed_stage[stage] = by_progressed_stage.get(stage, 0) + int(count)
        by_blocked_dispatch_stage: dict[str, int] = {}
        by_ready_dispatch_stage: dict[str, int] = {}
        for dispatch in dispatches:
            if dispatch["status"] == "blocked":
                by_blocked_dispatch_stage[dispatch["stage"]] = (
                    by_blocked_dispatch_stage.get(dispatch["stage"], 0) + 1
                )
            if dispatch["status"] == "ready":
                by_ready_dispatch_stage[dispatch["stage"]] = (
                    by_ready_dispatch_stage.get(dispatch["stage"], 0) + 1
                )
        production_handoffs = [
            attempt["production_handoff"]
            for attempt in attempts
            if isinstance(attempt.get("production_handoff"), dict)
        ]
        by_production_handoff_status: dict[str, int] = {}
        by_production_handoff_blocker: dict[str, int] = {}
        for handoff in production_handoffs:
            status = str(handoff.get("status") or "unknown")
            by_production_handoff_status[status] = (
                by_production_handoff_status.get(status, 0) + 1
            )
            blockers = handoff.get("blocking_reasons", [])
            if not isinstance(blockers, list):
                continue
            for blocker in blockers:
                blocker_key = str(blocker or "unknown")
                by_production_handoff_blocker[blocker_key] = (
                    by_production_handoff_blocker.get(blocker_key, 0) + 1
                )
        latest_attempt = max(attempts, key=lambda attempt: attempt["recorded_at"], default=None)
        latest_production_handoff_attempt = max(
            (
                attempt
                for attempt in attempts
                if isinstance(attempt.get("production_handoff"), dict)
            ),
            key=lambda attempt: attempt["recorded_at"],
            default=None,
        )
        latest_dispatch = max(
            dispatches,
            key=lambda dispatch: dispatch["requested_at"],
            default=None,
        )
        active_production_episode_ids = {
            entry["episode_id"]
            for entry in (self._production_run_entry(episode) for episode in episodes)
            if entry is not None and entry.get("active") is True
        }
        active_episode_by_id = {
            str(episode.id): episode
            for episode in episodes
            if str(episode.id) in active_production_episode_ids
        }
        current_attempts = self._latest_orchestration_entries_by_episode(
            [
                attempt
                for attempt in attempts
                if attempt["episode_id"] in active_production_episode_ids
            ]
        )
        current_dispatches = self._current_temporal_dispatch_entries(
            [
                dispatch
                for dispatch in dispatches
                if dispatch["episode_id"] in active_production_episode_ids
            ],
            current_attempts,
        )
        current_production_handoffs = [
            attempt["production_handoff"]
            for attempt in current_attempts
            if isinstance(attempt.get("production_handoff"), dict)
        ]
        current_error_count = sum(attempt["error_count"] for attempt in current_attempts)
        current_failed_stage_count = sum(
            attempt["failed_stage_count"] for attempt in current_attempts
        )
        current_blocked_dispatch_count = sum(
            1 for dispatch in current_dispatches if dispatch["status"] == "blocked"
        )
        current_waiting_media_handoffs = [
            handoff
            for handoff in current_production_handoffs
            if str(handoff.get("status") or "unknown") == "blocked"
            and (episode := active_episode_by_id.get(str(handoff.get("episode_id"))))
            is not None
            and self._episode_has_active_media_jobs(episode)
        ]
        current_waiting_media_handoff_ids = {
            id(handoff) for handoff in current_waiting_media_handoffs
        }
        current_waiting_action_handoffs = [
            handoff
            for handoff in current_production_handoffs
            if str(handoff.get("status") or "unknown") == "blocked"
            and id(handoff) not in current_waiting_media_handoff_ids
        ]
        current_waiting_handoffs = (
            current_waiting_media_handoffs + current_waiting_action_handoffs
        )
        current_waiting_handoff_ids = {id(handoff) for handoff in current_waiting_handoffs}
        current_blocked_handoff_count = sum(
            1
            for handoff in current_production_handoffs
            if str(handoff.get("status") or "unknown") == "blocked"
            and id(handoff) not in current_waiting_handoff_ids
        )
        error_count = sum(attempt["error_count"] for attempt in attempts)
        blocked_dispatch_count = by_dispatch_status.get("blocked", 0)
        blocked_handoff_count = by_production_handoff_status.get("blocked", 0)
        return {
            "schema_version": "workflow_orchestration_summary.v1",
            "attempt_count": len(attempts),
            "progressed_stage_count": sum(
                attempt["progressed_stage_count"] for attempt in attempts
            ),
            "error_count": error_count,
            "failed_stage_count": sum(attempt["failed_stage_count"] for attempt in attempts),
            "current_attempt_count": len(current_attempts),
            "current_error_count": current_error_count,
            "current_failed_stage_count": current_failed_stage_count,
            "dispatch_count": len(dispatches),
            "ready_dispatch_count": by_dispatch_status.get("ready", 0),
            "blocked_dispatch_count": blocked_dispatch_count,
            "current_dispatch_count": len(current_dispatches),
            "current_blocked_dispatch_count": current_blocked_dispatch_count,
            "production_handoff_count": len(production_handoffs),
            "blocked_production_handoff_count": blocked_handoff_count,
            "current_production_handoff_count": len(current_production_handoffs),
            "current_blocked_production_handoff_count": current_blocked_handoff_count,
            "current_waiting_production_handoff_count": len(current_waiting_handoffs),
            "current_waiting_media_handoff_count": len(current_waiting_media_handoffs),
            "current_waiting_action_handoff_count": len(current_waiting_action_handoffs),
            "review_ready_production_handoff_count": by_production_handoff_status.get(
                "review_ready",
                0,
            ),
            "delivery_ready_production_handoff_count": by_production_handoff_status.get(
                "delivery_ready",
                0,
            ),
            "by_worker": dict(sorted(by_worker.items())),
            "by_policy": dict(sorted(by_policy.items())),
            "by_dispatch_status": dict(sorted(by_dispatch_status.items())),
            "by_failed_stage": dict(sorted(by_failed_stage.items())),
            "by_progressed_stage": dict(sorted(by_progressed_stage.items())),
            "by_blocked_dispatch_stage": dict(sorted(by_blocked_dispatch_stage.items())),
            "by_ready_dispatch_stage": dict(sorted(by_ready_dispatch_stage.items())),
            "by_production_handoff_status": dict(
                sorted(by_production_handoff_status.items())
            ),
            "by_production_handoff_blocker": dict(
                sorted(by_production_handoff_blocker.items())
            ),
            "latest_attempt": latest_attempt,
            "latest_production_handoff": (
                latest_production_handoff_attempt.get("production_handoff")
                if latest_production_handoff_attempt is not None
                else None
            ),
            "latest_dispatch": latest_dispatch,
            "reason": (
                "workflow orchestration has current unresolved errors, blocked Temporal "
                "dispatches, or blocked production handoffs"
                if current_error_count > 0
                or current_blocked_dispatch_count > 0
                or current_blocked_handoff_count > 0
                else (
                    "workflow orchestration has no current unresolved errors or blocked "
                    "production handoffs"
                )
            ),
        }

    def _latest_orchestration_entries_by_episode(self, attempts: list[dict]) -> list[dict]:
        latest_by_episode: dict[str, dict] = {}
        for attempt in attempts:
            episode_id = attempt["episode_id"]
            existing = latest_by_episode.get(episode_id)
            if existing is None or attempt["recorded_at"] > existing["recorded_at"]:
                latest_by_episode[episode_id] = attempt
        return list(latest_by_episode.values())

    def _current_temporal_dispatch_entries(
        self,
        dispatches: list[dict],
        current_attempts: list[dict],
    ) -> list[dict]:
        latest_dispatch_by_episode: dict[str, dict] = {}
        for dispatch in dispatches:
            episode_id = dispatch["episode_id"]
            existing = latest_dispatch_by_episode.get(episode_id)
            if existing is None or dispatch["requested_at"] > existing["requested_at"]:
                latest_dispatch_by_episode[episode_id] = dispatch
        current_attempt_by_episode = {
            attempt["episode_id"]: attempt for attempt in current_attempts
        }
        current_dispatches: list[dict] = []
        for episode_id, dispatch in latest_dispatch_by_episode.items():
            attempt = current_attempt_by_episode.get(episode_id)
            if (
                attempt is not None
                and attempt["recorded_at"] > dispatch["requested_at"]
                and attempt["error_count"] == 0
                and str(
                    (attempt.get("production_handoff") or {}).get("status")
                    if isinstance(attempt.get("production_handoff"), dict)
                    else ""
                )
                != "blocked"
            ):
                continue
            current_dispatches.append(dispatch)
        return current_dispatches

    def _workflow_orchestration_entries(self, episodes: list[Episode]) -> list[dict]:
        attempts: list[dict] = []
        for episode in episodes:
            log = episode.workflow_control.get("worker_orchestration_log", [])
            if not isinstance(log, list):
                continue
            for item in log:
                if not isinstance(item, dict):
                    continue
                stage_attempts = [
                    stage_attempt
                    for stage_attempt in item.get("stage_attempts", [])
                    if isinstance(stage_attempt, dict)
                ]
                by_failed_stage: dict[str, int] = {}
                by_progressed_stage: dict[str, int] = {}
                for stage_attempt in stage_attempts:
                    stage = str(stage_attempt.get("stage") or "unknown")
                    status = str(stage_attempt.get("status") or "unknown")
                    if status == "failed":
                        by_failed_stage[stage] = by_failed_stage.get(stage, 0) + 1
                    if status == "progressed":
                        by_progressed_stage[stage] = by_progressed_stage.get(stage, 0) + 1
                production_handoff = item.get("production_handoff")
                if not isinstance(production_handoff, dict):
                    production_handoff = None
                entry = {
                    "episode_id": str(episode.id),
                    "summary_id": str(item.get("summary_id") or ""),
                    "attempt_sequence": int(item.get("attempt_sequence") or 0),
                    "recorded_at": str(item.get("recorded_at") or ""),
                    "worker_id": str(item.get("worker_id") or "unknown"),
                    "policy": str(item.get("policy") or "unknown"),
                    "progressed_stage_count": int(item.get("progressed_stage_count") or 0),
                    "error_count": int(item.get("error_count") or 0),
                    "failed_stage_count": sum(
                        1
                        for stage_attempt in stage_attempts
                        if stage_attempt.get("status") == "failed"
                    ),
                    "by_failed_stage": dict(sorted(by_failed_stage.items())),
                    "by_progressed_stage": dict(sorted(by_progressed_stage.items())),
                    "temporal_dispatch_count": int(item.get("temporal_dispatch_count") or 0),
                }
                if production_handoff is not None:
                    entry["production_handoff"] = production_handoff
                attempts.append(
                    entry
                )
        return attempts

    def _temporal_dispatch_entries(self, episodes: list[Episode]) -> list[dict]:
        dispatches: list[dict] = []
        for episode in episodes:
            log = episode.workflow_control.get("temporal_stage_dispatch_log", [])
            if not isinstance(log, list):
                continue
            for item in log:
                if not isinstance(item, dict):
                    continue
                dispatches.append(
                    {
                        "episode_id": str(episode.id),
                        "dispatch_id": str(item.get("dispatch_id") or ""),
                        "dispatch_sequence": int(item.get("dispatch_sequence") or 0),
                        "requested_at": str(item.get("requested_at") or ""),
                        "requested_by": str(item.get("requested_by") or "unknown"),
                        "status": str(item.get("status") or "unknown"),
                        "stage": str(item.get("stage") or "unknown"),
                        "activity_name": str(item.get("activity_name") or ""),
                        "namespace": str(item.get("namespace") or ""),
                        "task_queue": str(item.get("task_queue") or ""),
                    }
                )
        return dispatches

    def _workflow_retry_counts(self, episodes: list[Episode]) -> dict:
        retries = self._workflow_retry_entries(episodes)
        return {
            "workflow_stage_retries": len(retries),
            "scheduled_workflow_stage_retries": sum(
                1 for retry in retries if retry["status"] == "scheduled"
            ),
            "exhausted_workflow_stage_retries": sum(
                1 for retry in retries if retry["status"] == "exhausted"
            ),
            "due_workflow_stage_retries": sum(
                1 for retry in retries if retry.get("schedule_status") == "due"
            ),
        }

    def _model_generation_observability_summary(self, episodes: list[Episode]) -> dict:
        by_provider_type: dict[str, dict[str, Any]] = {}
        turn_count = 0
        latency_recorded_turn_count = 0
        token_usage_recorded_turn_count = 0
        latency_sum_ms = 0.0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0

        for episode in episodes:
            session = episode.discussion_session
            if session is None:
                continue
            for turn in session.turns:
                metadata = turn.generation_metadata or {}
                turn_count += 1
                provider_type = self._generation_provider_type(metadata)
                provider_summary = by_provider_type.setdefault(
                    provider_type,
                    {
                        "turn_count": 0,
                        "latency_recorded_turn_count": 0,
                        "token_usage_recorded_turn_count": 0,
                        "latency_sum_ms": 0.0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                )
                provider_summary["turn_count"] += 1

                latency_ms = self._metadata_float(metadata.get("model_latency_ms"))
                if latency_ms is not None:
                    latency_recorded_turn_count += 1
                    latency_sum_ms += latency_ms
                    provider_summary["latency_recorded_turn_count"] += 1
                    provider_summary["latency_sum_ms"] += latency_ms

                usage = metadata.get("token_usage")
                if isinstance(usage, dict) and metadata.get("token_usage_available") is True:
                    token_usage_recorded_turn_count += 1
                    provider_summary["token_usage_recorded_turn_count"] += 1
                    prompt_tokens = self._metadata_int(usage.get("prompt_tokens")) or 0
                    completion_tokens = self._metadata_int(usage.get("completion_tokens")) or 0
                    usage_total_tokens = self._metadata_int(usage.get("total_tokens")) or (
                        prompt_tokens + completion_tokens
                    )
                    total_prompt_tokens += prompt_tokens
                    total_completion_tokens += completion_tokens
                    total_tokens += usage_total_tokens
                    provider_summary["prompt_tokens"] += prompt_tokens
                    provider_summary["completion_tokens"] += completion_tokens
                    provider_summary["total_tokens"] += usage_total_tokens

        for provider_summary in by_provider_type.values():
            latency_count = provider_summary["latency_recorded_turn_count"]
            provider_summary["average_model_latency_ms"] = (
                round(provider_summary["latency_sum_ms"] / latency_count, 3)
                if latency_count
                else None
            )
            provider_summary["latency_sum_ms"] = round(provider_summary["latency_sum_ms"], 3)

        return {
            "turn_count": turn_count,
            "latency_recorded_turn_count": latency_recorded_turn_count,
            "token_usage_recorded_turn_count": token_usage_recorded_turn_count,
            "model_latency_sum_ms": round(latency_sum_ms, 3),
            "average_model_latency_ms": (
                round(latency_sum_ms / latency_recorded_turn_count, 3)
                if latency_recorded_turn_count
                else None
            ),
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
            "by_provider_type": dict(sorted(by_provider_type.items())),
        }

    def _model_generation_observability_check(self, summary: dict) -> dict:
        readiness_checks = {
            "model_latency_recorded_for_turns": summary["turn_count"] == 0
            or summary["latency_recorded_turn_count"] == summary["turn_count"],
            "token_usage_aggregation_available": True,
        }
        ready = all(readiness_checks.values())
        return {
            "name": "model_generation_observability",
            "status": "healthy" if ready else "degraded",
            "details": {
                "schema_version": "model_generation_observability.v1",
                **summary,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [
                    name for name, passed in readiness_checks.items() if not passed
                ],
                "reason": (
                    "model gateway latency and token-usage metadata is aggregated"
                    if ready
                    else "some persisted discussion turns are missing model latency metadata"
                ),
            },
        }

    def _model_generation_observability_counts(self, summary: dict) -> dict:
        return {
            "model_generation_turns": int(summary["turn_count"]),
            "model_generation_latency_records": int(summary["latency_recorded_turn_count"]),
            "model_generation_token_usage_records": int(summary["token_usage_recorded_turn_count"]),
        }

    def _generation_provider_type(self, metadata: dict[str, Any]) -> str:
        value = metadata.get("provider_type")
        if hasattr(value, "value"):
            value = value.value
        if isinstance(value, str) and value:
            return value
        return "unknown"

    def _metadata_float(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        return None

    def _metadata_int(self, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None

    def _asset_production_observability_summary(self, episodes: list[Episode]) -> dict:
        assets = [asset for episode in episodes for asset in episode.assets]
        by_asset_type: dict[str, dict[str, Any]] = {}
        by_language: dict[str, dict[str, Any]] = {}
        duration_recorded_asset_count = 0
        size_recorded_asset_count = 0
        duration_sum_ms = 0
        storage_size_bytes = 0

        for asset in assets:
            asset_type = self._asset_type_value(asset.asset_type)
            language = asset.language or "und"
            asset_type_summary = by_asset_type.setdefault(
                asset_type,
                self._empty_asset_observability_bucket(),
            )
            language_summary = by_language.setdefault(
                language,
                self._empty_asset_observability_bucket(),
            )
            for bucket in (asset_type_summary, language_summary):
                bucket["asset_count"] += 1
                if asset.status == "completed":
                    bucket["completed_asset_count"] += 1
                if asset.status == "failed":
                    bucket["failed_asset_count"] += 1

            duration_ms = self._metadata_int(asset.duration_ms)
            if duration_ms is not None:
                duration_recorded_asset_count += 1
                duration_sum_ms += duration_ms
                for bucket in (asset_type_summary, language_summary):
                    bucket["duration_recorded_asset_count"] += 1
                    bucket["duration_sum_ms"] += duration_ms

            size_bytes = self._asset_size_bytes(asset)
            if size_bytes is not None:
                size_recorded_asset_count += 1
                storage_size_bytes += size_bytes
                for bucket in (asset_type_summary, language_summary):
                    bucket["size_recorded_asset_count"] += 1
                    bucket["storage_size_bytes"] += size_bytes

        for bucket in [*by_asset_type.values(), *by_language.values()]:
            asset_count = bucket["asset_count"]
            completed_count = bucket["completed_asset_count"]
            failed_count = bucket["failed_asset_count"]
            bucket["completion_rate"] = (
                round(completed_count / asset_count, 6) if asset_count else 0.0
            )
            bucket["failure_rate"] = round(failed_count / asset_count, 6) if asset_count else 0.0

        asset_count = len(assets)
        completed_asset_count = sum(1 for asset in assets if asset.status == "completed")
        failed_asset_count = sum(1 for asset in assets if asset.status == "failed")
        return {
            "asset_count": asset_count,
            "completed_asset_count": completed_asset_count,
            "failed_asset_count": failed_asset_count,
            "duration_recorded_asset_count": duration_recorded_asset_count,
            "size_recorded_asset_count": size_recorded_asset_count,
            "duration_sum_ms": duration_sum_ms,
            "storage_size_bytes": storage_size_bytes,
            "completion_rate": round(completed_asset_count / asset_count, 6)
            if asset_count
            else 0.0,
            "failure_rate": round(failed_asset_count / asset_count, 6) if asset_count else 0.0,
            "by_asset_type": dict(sorted(by_asset_type.items())),
            "by_language": dict(sorted(by_language.items())),
        }

    def _asset_production_observability_check(self, summary: dict) -> dict:
        readiness_checks = {
            "asset_duration_aggregation_available": True,
            "asset_storage_size_aggregation_available": True,
        }
        return {
            "name": "asset_production_observability",
            "status": "healthy",
            "details": {
                "schema_version": "asset_production_observability.v1",
                **summary,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [],
                "reason": "asset duration, size, status, type, and language evidence is aggregated",
            },
        }

    def _asset_production_observability_counts(self, summary: dict) -> dict:
        return {
            "production_assets": int(summary["asset_count"]),
            "completed_production_assets": int(summary["completed_asset_count"]),
            "failed_production_assets": int(summary["failed_asset_count"]),
            "production_asset_duration_records": int(summary["duration_recorded_asset_count"]),
            "production_asset_size_records": int(summary["size_recorded_asset_count"]),
        }

    def _empty_asset_observability_bucket(self) -> dict[str, Any]:
        return {
            "asset_count": 0,
            "completed_asset_count": 0,
            "failed_asset_count": 0,
            "duration_recorded_asset_count": 0,
            "size_recorded_asset_count": 0,
            "duration_sum_ms": 0,
            "storage_size_bytes": 0,
        }

    def _asset_type_value(self, value: Any) -> str:
        if hasattr(value, "value"):
            value = value.value
        if isinstance(value, str) and value:
            return value
        return "unknown"

    def _asset_size_bytes(self, asset: Any) -> int | None:
        metadata = asset.generation_metadata or {}
        direct = self._metadata_int(metadata.get("object_size_bytes"))
        if direct is not None:
            return direct
        media_probe = metadata.get("media_probe")
        if isinstance(media_probe, dict):
            probed = self._metadata_int(media_probe.get("size_bytes"))
            if probed is not None:
                return probed
        path_value = metadata.get("object_storage_path")
        if isinstance(path_value, str) and path_value:
            try:
                path = Path(path_value)
                if path.exists() and path.is_file():
                    return path.stat().st_size
            except OSError:
                return None
        return None

    def _queue_wait_observability_summary(self, episodes: list[Episode]) -> dict:
        now = datetime.now(UTC)
        by_queue: dict[str, dict[str, int]] = {}
        by_language: dict[str, dict[str, int]] = {}
        pending_wait_ms_sum = 0
        pending_wait_record_count = 0
        completed_wait_ms_sum = 0
        completed_wait_record_count = 0

        for episode in episodes:
            for asset in episode.assets:
                queue_name = self._asset_type_value(asset.asset_type)
                language = asset.language or "und"
                submitted_at = self._asset_submitted_at(asset)
                completed_at = self._asset_completed_at(asset)
                if asset.status in {"submitted", "running"} and submitted_at is not None:
                    wait_ms = self._duration_ms_between(submitted_at, now)
                    if wait_ms is not None:
                        pending_wait_ms_sum += wait_ms
                        pending_wait_record_count += 1
                        self._add_queue_wait(
                            by_queue,
                            queue_name,
                            wait_ms,
                            pending=True,
                        )
                        self._add_queue_wait(
                            by_language,
                            language,
                            wait_ms,
                            pending=True,
                        )
                if (
                    asset.status == "completed"
                    and submitted_at is not None
                    and completed_at is not None
                ):
                    wait_ms = self._duration_ms_between(submitted_at, completed_at)
                    if wait_ms is not None:
                        completed_wait_ms_sum += wait_ms
                        completed_wait_record_count += 1
                        self._add_queue_wait(
                            by_queue,
                            queue_name,
                            wait_ms,
                            pending=False,
                        )
                        self._add_queue_wait(
                            by_language,
                            language,
                            wait_ms,
                            pending=False,
                        )

            for job in episode.publish_jobs:
                if job.status == "submitted":
                    wait_ms = self._duration_ms_between(job.requested_at, now)
                    if wait_ms is not None:
                        pending_wait_ms_sum += wait_ms
                        pending_wait_record_count += 1
                        self._add_queue_wait(
                            by_queue,
                            "publish_job",
                            wait_ms,
                            pending=True,
                        )
                if job.status == "completed" and job.completed_at is not None:
                    wait_ms = self._duration_ms_between(job.requested_at, job.completed_at)
                    if wait_ms is not None:
                        completed_wait_ms_sum += wait_ms
                        completed_wait_record_count += 1
                        self._add_queue_wait(
                            by_queue,
                            "publish_job",
                            wait_ms,
                            pending=False,
                        )

        return {
            "schema_version": "queue_wait_observability.v1",
            "pending_wait_ms_sum": pending_wait_ms_sum,
            "pending_wait_record_count": pending_wait_record_count,
            "completed_wait_ms_sum": completed_wait_ms_sum,
            "completed_wait_record_count": completed_wait_record_count,
            "by_queue": dict(sorted(by_queue.items())),
            "by_language": dict(sorted(by_language.items())),
            "timestamp_sources": {
                "asset_submitted_at": [
                    "generation_metadata.submitted_at",
                    "created_at for already submitted or running assets without submitted_at",
                ],
                "asset_completed_at": [
                    "generation_metadata.completed_at",
                    "updated_at for completed assets without completed_at",
                ],
                "publish_job": ["requested_at", "completed_at"],
            },
        }

    def _queue_wait_observability_check(self, summary: dict) -> dict:
        readiness_checks = {
            "queue_wait_aggregation_available": True,
            "pending_queue_wait_records_available": True,
            "completed_queue_wait_records_available": True,
        }
        return {
            "name": "queue_wait_observability",
            "status": "healthy",
            "details": {
                **summary,
                "readiness_checks": readiness_checks,
                "failed_readiness_checks": [],
                "reason": (
                    "pending and completed queue wait spans are aggregated from "
                    "persisted asset and publish-job timestamps"
                ),
            },
        }

    def _queue_wait_observability_counts(self, summary: dict) -> dict:
        return {
            "pending_queue_wait_records": int(summary["pending_wait_record_count"]),
            "completed_queue_wait_records": int(summary["completed_wait_record_count"]),
        }

    def _add_queue_wait(
        self,
        buckets: dict[str, dict[str, int]],
        key: str,
        wait_ms: int,
        *,
        pending: bool,
    ) -> None:
        bucket = buckets.setdefault(
            key,
            {
                "pending_wait_ms_sum": 0,
                "pending_wait_record_count": 0,
                "completed_wait_ms_sum": 0,
                "completed_wait_record_count": 0,
            },
        )
        if pending:
            bucket["pending_wait_ms_sum"] += wait_ms
            bucket["pending_wait_record_count"] += 1
        else:
            bucket["completed_wait_ms_sum"] += wait_ms
            bucket["completed_wait_record_count"] += 1

    def _asset_submitted_at(self, asset: Any) -> datetime | None:
        metadata = asset.generation_metadata or {}
        submitted_at = self._parse_timestamp(metadata.get("submitted_at"))
        if submitted_at is not None:
            return submitted_at
        if asset.status in {"submitted", "running"}:
            return self._parse_timestamp(asset.created_at)
        return None

    def _asset_completed_at(self, asset: Any) -> datetime | None:
        metadata = asset.generation_metadata or {}
        completed_at = self._parse_timestamp(metadata.get("completed_at"))
        if completed_at is not None:
            return completed_at
        if asset.status == "completed":
            return self._parse_timestamp(asset.updated_at)
        return None

    def _workflow_retry_summary(self, episodes: list[Episode]) -> dict:
        retries = self._workflow_retry_entries(episodes)
        history = self._workflow_retry_history_entries(episodes)
        resolved_history = [
            retry
            for retry in history
            if retry["status"]
            in {"operator_retried", "manual_edit_resolved", "operator_acknowledged"}
        ]
        by_resolution_status: dict[str, int] = {}
        by_resolution_stage: dict[str, int] = {}
        for retry in resolved_history:
            by_resolution_status[retry["status"]] = by_resolution_status.get(retry["status"], 0) + 1
            by_resolution_stage[retry["stage"]] = by_resolution_stage.get(retry["stage"], 0) + 1
        by_status: dict[str, int] = {}
        by_stage: dict[str, int] = {}
        by_schedule_status: dict[str, int] = {}
        by_due_stage: dict[str, int] = {}
        by_backoff_stage: dict[str, int] = {}
        by_unknown_schedule_stage: dict[str, int] = {}
        by_exhausted_stage: dict[str, int] = {}
        for retry in retries:
            by_status[retry["status"]] = by_status.get(retry["status"], 0) + 1
            by_stage[retry["stage"]] = by_stage.get(retry["stage"], 0) + 1
            schedule_status = str(retry.get("schedule_status") or "unknown")
            by_schedule_status[schedule_status] = by_schedule_status.get(schedule_status, 0) + 1
            if schedule_status == "due":
                by_due_stage[retry["stage"]] = by_due_stage.get(retry["stage"], 0) + 1
            elif schedule_status == "backoff":
                by_backoff_stage[retry["stage"]] = by_backoff_stage.get(retry["stage"], 0) + 1
            elif schedule_status == "unknown":
                by_unknown_schedule_stage[retry["stage"]] = (
                    by_unknown_schedule_stage.get(retry["stage"], 0) + 1
                )
            if retry["status"] == "exhausted":
                by_exhausted_stage[retry["stage"]] = by_exhausted_stage.get(retry["stage"], 0) + 1
        latest = max(retries, key=lambda retry: retry["created_at"], default=None)
        scheduled_retries = [retry for retry in retries if retry["status"] == "scheduled"]
        next_retry = min(
            (
                retry
                for retry in scheduled_retries
                if retry.get("next_retry_not_before") is not None
            ),
            key=self._workflow_retry_timestamp_sort_key,
            default=None,
        )
        return {
            "schema_version": "workflow_retry_summary.v1",
            "total_retry_entries": len(retries),
            "historical_retry_entries": len(history),
            "resolved_retry_entries": len(resolved_history),
            "scheduled_retry_entries": by_status.get("scheduled", 0),
            "exhausted_retry_entries": by_status.get("exhausted", 0),
            "due_retry_entries": by_schedule_status.get("due", 0),
            "backoff_retry_entries": by_schedule_status.get("backoff", 0),
            "unknown_schedule_retry_entries": by_schedule_status.get("unknown", 0),
            "by_status": dict(sorted(by_status.items())),
            "by_stage": dict(sorted(by_stage.items())),
            "by_schedule_status": dict(sorted(by_schedule_status.items())),
            "by_due_stage": dict(sorted(by_due_stage.items())),
            "by_backoff_stage": dict(sorted(by_backoff_stage.items())),
            "by_unknown_schedule_stage": dict(sorted(by_unknown_schedule_stage.items())),
            "by_exhausted_stage": dict(sorted(by_exhausted_stage.items())),
            "by_resolution_status": dict(sorted(by_resolution_status.items())),
            "by_resolution_stage": dict(sorted(by_resolution_stage.items())),
            "latest_retry": latest,
            "next_retry": next_retry,
            "next_retry_not_before": (
                next_retry.get("next_retry_not_before") if next_retry else None
            ),
            "reason": (
                "workflow stage retries are pending or exhausted"
                if retries
                else "no workflow stage retries are queued"
            ),
        }

    def _workflow_retry_entries(self, episodes: list[Episode]) -> list[dict]:
        return [
            retry
            for retry in self._workflow_retry_history_entries(episodes)
            if retry["status"]
            not in {
                "operator_retried",
                "manual_edit_resolved",
                "automatic_retried",
                "operator_acknowledged",
            }
        ]

    def _workflow_retry_history_entries(self, episodes: list[Episode]) -> list[dict]:
        retries: list[dict] = []
        for episode in episodes:
            retry_queue = episode.workflow_control.get("stage_retry_queue", [])
            if not isinstance(retry_queue, list):
                continue
            for item in retry_queue:
                if not isinstance(item, dict):
                    continue
                retry = {
                    "episode_id": str(episode.id),
                    "retry_id": str(item.get("retry_id") or ""),
                    "stage": str(item.get("stage") or "unknown"),
                    "target_stage": str(item.get("target_stage") or ""),
                    "status": str(item.get("status") or "unknown"),
                    "attempt_number": int(item.get("attempt_number") or 0),
                    "max_attempts": int(item.get("max_attempts") or 0),
                    "created_at": str(item.get("created_at") or ""),
                    "next_retry_not_before": item.get("next_retry_not_before"),
                    "resolved_at": item.get("resolved_at"),
                    "resolved_by": item.get("resolved_by"),
                    "previous_status": item.get("previous_status"),
                }
                retry["schedule_status"] = self._workflow_retry_schedule_status(retry)
                retries.append(retry)
        return retries

    def _workflow_retry_schedule_status(self, retry: dict) -> str:
        if retry.get("status") != "scheduled":
            return "not_scheduled"
        next_retry_not_before = retry.get("next_retry_not_before")
        retry_at = self._workflow_retry_timestamp(next_retry_not_before)
        if retry_at is None:
            return "unknown"
        return "due" if retry_at <= datetime.now(UTC) else "backoff"

    def _workflow_retry_entry_sort_key(self, retry: dict) -> tuple[int, datetime, str]:
        schedule_rank = {
            "due": 0,
            "backoff": 1,
            "unknown": 2,
            "not_scheduled": 3,
        }.get(str(retry.get("schedule_status") or "unknown"), 2)
        next_retry = self._workflow_retry_timestamp_sort_key(retry)
        if next_retry == datetime.max.replace(tzinfo=UTC):
            next_retry = self._workflow_retry_timestamp(retry.get("created_at")) or next_retry
        return (schedule_rank, next_retry, str(retry.get("retry_id") or ""))

    def _timestamp_sort_key(self, value: object) -> datetime:
        return self._workflow_retry_timestamp(value) or datetime.min.replace(tzinfo=UTC)

    def _workflow_retry_timestamp_sort_key(self, retry: dict) -> datetime:
        parsed = self._workflow_retry_timestamp(retry.get("next_retry_not_before"))
        if parsed is None:
            return datetime.max.replace(tzinfo=UTC)
        return parsed

    def _workflow_retry_timestamp(self, value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    def _queue_summary(self, episodes: list[Episode]) -> dict:
        media_queue_asset_types = {
            AssetType.audio,
            AssetType.subtitle,
            AssetType.video,
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
            AssetType.citation_card,
        }
        terminal_episode_ids = {
            episode.id
            for episode in episodes
            if str(getattr(episode.status, "value", episode.status)).lower()
            in {"cancelled", "failed"}
        }
        assets = [asset for episode in episodes for asset in episode.assets]
        current_assets = [
            asset
            for episode in episodes
            if episode.id not in terminal_episode_ids
            for asset in episode.assets
        ]
        audio_assets = [asset for asset in assets if asset.asset_type == AssetType.audio]
        subtitle_assets = [asset for asset in assets if asset.asset_type == AssetType.subtitle]
        visual_assets = [
            asset
            for asset in assets
            if asset.asset_type
            in {
                AssetType.video,
                AssetType.broll,
                AssetType.reaction_loop,
                AssetType.studio_scene,
                AssetType.citation_card,
            }
        ]
        render_assets = [asset for asset in assets if asset.asset_type == AssetType.render]
        failed_assets = [asset for asset in assets if asset.status == "failed"]
        current_audio_assets = [
            asset for asset in current_assets if asset.asset_type == AssetType.audio
        ]
        current_subtitle_assets = [
            asset for asset in current_assets if asset.asset_type == AssetType.subtitle
        ]
        current_visual_assets = [
            asset
            for asset in current_assets
            if asset.asset_type
            in {
                AssetType.video,
                AssetType.broll,
                AssetType.reaction_loop,
                AssetType.studio_scene,
                AssetType.citation_card,
            }
        ]
        current_failed_assets = [
            asset
            for episode in episodes
            if episode.id not in terminal_episode_ids
            for asset in episode.assets
            if asset.status == "failed"
            and asset.asset_type in media_queue_asset_types
            and self._asset_matches_current_transcript(episode, asset)
            and self._asset_blocks_current_production(asset)
        ]
        nonblocking_current_failed_assets = [
            asset
            for episode in episodes
            if episode.id not in terminal_episode_ids
            for asset in episode.assets
            if asset.status == "failed"
            and asset.asset_type in media_queue_asset_types
            and self._asset_matches_current_transcript(episode, asset)
            and not self._asset_blocks_current_production(asset)
        ]
        stale_revision_failed_assets = [
            asset
            for episode in episodes
            if episode.id not in terminal_episode_ids
            for asset in episode.assets
            if asset.status == "failed"
            and asset.asset_type in media_queue_asset_types
            and not self._asset_matches_current_transcript(episode, asset)
        ]
        current_failed_render_assets = [
            asset
            for episode in episodes
            if episode.id not in terminal_episode_ids
            for asset in episode.assets
            if asset.status == "failed"
            and asset.asset_type == AssetType.render
            and self._asset_matches_current_transcript(episode, asset)
        ]
        return {
            "pending_audio_jobs": sum(
                1 for asset in audio_assets if asset.status in {"submitted", "running"}
            ),
            "submitted_audio_jobs": sum(1 for asset in audio_assets if asset.status == "submitted"),
            "running_audio_jobs": sum(1 for asset in audio_assets if asset.status == "running"),
            "pending_visual_jobs": sum(
                1 for asset in visual_assets if asset.status in {"submitted", "running"}
            ),
            "submitted_visual_jobs": sum(
                1 for asset in visual_assets if asset.status == "submitted"
            ),
            "running_visual_jobs": sum(1 for asset in visual_assets if asset.status == "running"),
            "pending_subtitle_jobs": sum(
                1 for asset in subtitle_assets if asset.status in {"submitted", "running"}
            ),
            "submitted_subtitle_jobs": sum(
                1 for asset in subtitle_assets if asset.status == "submitted"
            ),
            "running_subtitle_jobs": sum(
                1 for asset in subtitle_assets if asset.status == "running"
            ),
            "planned_audio_assets": sum(1 for asset in audio_assets if asset.status == "planned"),
            "planned_visual_assets": sum(1 for asset in visual_assets if asset.status == "planned"),
            "planned_subtitle_assets": sum(
                1 for asset in subtitle_assets if asset.status == "planned"
            ),
            "completed_renders": sum(1 for asset in render_assets if asset.status == "completed"),
            "failed_assets": len(failed_assets),
            "failed_audio_assets": sum(1 for asset in audio_assets if asset.status == "failed"),
            "failed_visual_assets": sum(1 for asset in visual_assets if asset.status == "failed"),
            "failed_subtitle_assets": sum(
                1 for asset in subtitle_assets if asset.status == "failed"
            ),
            "current_pending_audio_jobs": sum(
                1 for asset in current_audio_assets if asset.status in {"submitted", "running"}
            ),
            "current_pending_visual_jobs": sum(
                1 for asset in current_visual_assets if asset.status in {"submitted", "running"}
            ),
            "current_pending_subtitle_jobs": sum(
                1
                for asset in current_subtitle_assets
                if asset.status in {"submitted", "running"}
            ),
            "current_failed_assets": len(current_failed_assets),
            "current_nonblocking_failed_assets": len(nonblocking_current_failed_assets),
            "current_stale_revision_failed_assets": len(stale_revision_failed_assets),
            "current_failed_render_assets": len(current_failed_render_assets),
            "current_failed_audio_assets": sum(
                1 for asset in current_failed_assets if asset.asset_type == AssetType.audio
            ),
            "current_failed_visual_assets": sum(
                1
                for asset in current_failed_assets
                if asset.asset_type
                in {
                    AssetType.video,
                    AssetType.broll,
                    AssetType.reaction_loop,
                    AssetType.studio_scene,
                    AssetType.citation_card,
                }
            ),
            "current_failed_subtitle_assets": sum(
                1 for asset in current_failed_assets if asset.asset_type == AssetType.subtitle
            ),
        }

    @staticmethod
    def _asset_matches_current_transcript(episode: Episode, asset: Asset) -> bool:
        generation_metadata = (
            asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
        )
        transcript_version_id = str(generation_metadata.get("transcript_version_id") or "").strip()
        canonical_transcript_version_id = str(
            episode.canonical_transcript_version_id or ""
        ).strip()
        return not (
            transcript_version_id
            and canonical_transcript_version_id
            and transcript_version_id != canonical_transcript_version_id
        )

    @staticmethod
    def _asset_blocks_current_production(asset: Asset) -> bool:
        generation_metadata = (
            asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
        )
        required_for_production = generation_metadata.get("required_for_production")
        if isinstance(required_for_production, bool):
            return required_for_production
        return asset.asset_type not in {
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
            AssetType.citation_card,
        }

    def _settings_summary(self) -> dict:
        database_resolution_error: str | None = None
        try:
            database_driver = self.settings.resolved_database_url().split(
                ":",
                maxsplit=1,
            )[0]
        except RuntimeError as exc:
            database_driver = self.settings.database_driver.strip().split(
                ":",
                maxsplit=1,
            )[0]
            database_resolution_error = str(exc)
        return {
            "env": self.settings.env,
            "auth_enabled": self.settings.auth_enabled,
            "cors_allowed_origins": self.settings.resolved_cors_allowed_origins(),
            "auth_api_key_reference_configured": self._configured_string(
                self.settings.auth_api_key_reference
            ),
            "auth_role_header": self.settings.auth_role_header,
            "auth_user_header": self.settings.auth_user_header,
            "auth_trusted_identity_enabled": self.settings.auth_trusted_identity_enabled,
            "auth_trusted_identity_header": self.settings.auth_trusted_identity_header,
            "auth_trusted_email_header": self.settings.auth_trusted_email_header,
            "auth_trusted_groups_header": self.settings.auth_trusted_groups_header,
            "auth_trusted_default_role": self.settings.auth_trusted_default_role,
            "auth_trusted_group_role_map_configured": bool(
                self.settings.auth_trusted_group_role_map.strip()
            ),
            "auth_provider_session_enabled": self.settings.auth_provider_session_enabled,
            "auth_provider_session_introspection_configured": (
                self._configured_string(self.settings.auth_provider_session_introspection_url)
            ),
            "auth_provider_session_client_id_reference_configured": (
                self._configured_string(
                    self.settings.auth_provider_session_client_id_reference
                )
            ),
            "auth_provider_session_client_secret_reference_configured": (
                self._configured_string(
                    self.settings.auth_provider_session_client_secret_reference
                )
            ),
            "auth_provider_session_token_header": (
                self.settings.auth_provider_session_token_header
            ),
            "auth_provider_session_user_claim": (self.settings.auth_provider_session_user_claim),
            "auth_provider_session_groups_claim": (
                self.settings.auth_provider_session_groups_claim
            ),
            "auth_provider_session_default_role": (
                self.settings.auth_provider_session_default_role
            ),
            "auth_provider_session_group_role_map_configured": bool(
                self.settings.auth_provider_session_group_role_map.strip()
            ),
            "auth_provider_session_revocation_configured": (
                self._configured_string(self.settings.auth_provider_session_revocation_path)
            ),
            "auth_provider_session_decision_log_configured": (
                self._configured_string(self.settings.auth_provider_session_decision_log_path)
            ),
            "auth_provider_session_decision_log_limit": (
                self.settings.auth_provider_session_decision_log_limit
            ),
            "redis_url_configured": bool(self.settings.redis_url.strip()),
            "redis_event_fanout_enabled": self.settings.redis_event_fanout_enabled,
            "redis_event_channel": self.settings.redis_event_channel,
            "redis_worker_signal_enabled": self.settings.redis_worker_signal_enabled,
            "redis_worker_signal_stream": self.settings.redis_worker_signal_stream,
            "redis_worker_signal_maxlen": self.settings.redis_worker_signal_maxlen,
            "database_url_driver": database_driver,
            "database_url_resolved": database_resolution_error is None,
            "database_resolution_error": database_resolution_error,
            "object_storage_backend": self.settings.object_storage_backend,
            "object_storage_endpoint_configured": bool(
                self.settings.object_storage_endpoint.strip()
            ),
            "object_storage_bucket_configured": bool(
                self.settings.object_storage_bucket.strip()
            ),
            "object_storage_region_configured": bool(
                self.settings.object_storage_region.strip()
            ),
            "object_storage_access_key_reference_configured": bool(
                self._configured_string(self.settings.object_storage_access_key_reference)
            ),
            "object_storage_secret_key_reference_configured": bool(
                self._configured_string(self.settings.object_storage_secret_key_reference)
            ),
            "object_storage_credential_pair_configured": (
                self._configured_string(self.settings.object_storage_access_key_reference)
                == self._configured_string(self.settings.object_storage_secret_key_reference)
            ),
            "object_storage_force_path_style": self.settings.object_storage_force_path_style,
            "object_storage_auto_create_bucket": (
                self.settings.object_storage_auto_create_bucket
            ),
            "worker_poll_interval_seconds": self.settings.worker_poll_interval_seconds,
            "worker_sync_batch_limit": self.settings.worker_sync_batch_limit,
            "worker_heartbeat_ttl_seconds": self.settings.worker_heartbeat_ttl_seconds,
            "worker_lease_ttl_seconds": self.settings.worker_lease_ttl_seconds,
            "worker_runtime_state_retention_seconds": (
                self.settings.worker_runtime_state_retention_seconds
            ),
            "worker_auto_start_production_runs_enabled": (
                self.settings.worker_auto_start_production_runs_enabled
            ),
            "runtime_state_path_configured": bool(self.settings.runtime_state_path.strip()),
            "backup_path_configured": bool(self.settings.backup_path.strip()),
            "temporal_backend_mode": self.settings.temporal_backend_mode,
            "temporal_backend_address_configured": (
                self._configured_string(self.settings.temporal_backend_address)
            ),
            "temporal_backend_tls_enabled": self.settings.temporal_backend_tls_enabled,
            "temporal_backend_worker_enabled": (self.settings.temporal_backend_worker_enabled),
            "temporal_signal_transport_enabled": (self.settings.temporal_signal_transport_enabled),
            "temporal_signal_endpoint_configured": (
                self._configured_string(self.settings.temporal_signal_endpoint)
            ),
            "temporal_namespace": self.settings.temporal_namespace,
            "temporal_task_queue": self.settings.temporal_task_queue,
            "publisher_automated_live_enabled": (self.settings.publisher_automated_live_enabled),
            "research_retrieval_timeout_seconds": self.settings.research_retrieval_timeout_seconds,
            "research_retrieval_max_bytes": self.settings.research_retrieval_max_bytes,
        }

    @staticmethod
    def _configured_string(value: str | None) -> bool:
        return bool(value and value.strip())

    def _overall_status(self, components: list[dict], queues: dict) -> str:
        if any(component["status"] == "unhealthy" for component in components):
            return "unhealthy"
        if any(component["status"] in {"degraded", "unknown"} for component in components):
            return "degraded"
        if int(queues.get("failed_assets") or 0) > 0:
            return "degraded"
        return "healthy"
