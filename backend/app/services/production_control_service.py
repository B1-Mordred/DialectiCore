from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Asset,
    AuditEvent,
    Episode,
    WorkflowActionRequest,
    WorkflowRetryResolutionRequest,
)


def worker_stage_progress_count(stage: str, summary: dict) -> int:
    if stage == "research":
        return int(summary.get("evidence_packs_built") or 0)
    if stage == "discussion":
        return int(summary.get("discussions_completed") or 0)
    if stage == "localization":
        return int(summary.get("localized_languages_created") or 0)
    if stage == "qc":
        return int(summary.get("claim_qc_completed") or 0)
    if stage == "audio":
        return int(summary.get("episodes_generated") or 0)
    if stage == "visuals":
        return int(summary.get("episodes_generated") or 0)
    if stage in {"voicebox", "comfyui"}:
        return int(summary.get("episodes_synced") or 0)
    if stage == "subtitles":
        return int(summary.get("subtitles_generated") or 0)
    if stage == "timeline":
        return int(summary.get("timelines_built") or 0)
    if stage == "render":
        return int(summary.get("preview_renders_created") or 0) + int(
            summary.get("final_renders_created") or 0
        ) + int(summary.get("preview_render_requests_submitted") or 0) + int(
            summary.get("final_render_requests_submitted") or 0
        )
    if stage == "publishing":
        return (
            int(summary.get("thumbnails_created") or 0)
            + int(summary.get("youtube_packages_created") or 0)
            + int(summary.get("production_manifests_created") or 0)
            + int(summary.get("dry_run_publish_jobs_created") or 0)
            + int(summary.get("live_publish_jobs_created") or 0)
        )
    if stage == "completion":
        return int(summary.get("episodes_completed") or 0)
    return 0


class ProductionControlService:
    external_temporal_policy = "local_durable_temporal_signal_transport_replay_v1"
    worker_orchestration_log_retention_limit = 100
    temporal_stage_dispatch_log_retention_limit = 250

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    terminal_statuses = {
        EpisodeStatus.completed,
        EpisodeStatus.cancelled,
    }
    stage_plan = [
        ("definition_validated", EpisodeStatus.draft.value, "Validate episode definition"),
        ("research", EpisodeStatus.researching.value, "Build source evidence pack"),
        (
            "preparing_discussion",
            EpisodeStatus.preparing_discussion.value,
            "Prepare discussion session",
        ),
        (
            "discussion",
            EpisodeStatus.discussing.value,
            "Conduct multi-agent discussion",
        ),
        (
            "transcript_qc",
            EpisodeStatus.transcript_qc.value,
            "Run transcript quality checks",
        ),
        (
            "transcript_review",
            EpisodeStatus.transcript_review.value,
            "Review canonical transcript",
        ),
        ("localization", EpisodeStatus.localizing.value, "Prepare language variants"),
        ("audio", EpisodeStatus.generating_audio.value, "Generate speech assets"),
        ("subtitles", EpisodeStatus.ready.value, "Generate synchronized subtitle assets"),
        ("visuals", EpisodeStatus.generating_visuals.value, "Generate visual assets"),
        ("timeline", EpisodeStatus.building_timeline.value, "Build synchronized timeline"),
        ("render", EpisodeStatus.rendering_final.value, "Render final video"),
        ("delivery", EpisodeStatus.completed.value, "Package and publish delivery"),
    ]

    def apply_action(
        self,
        episode: Episode,
        request: WorkflowActionRequest,
    ) -> Episode:
        if request.action == "pause":
            return self.pause(episode, request)
        if request.action == "resume":
            return self.resume(episode, request)
        if request.action == "cancel":
            return self.cancel(episode, request)
        if request.action == "stop_run":
            return self.stop_run(episode, request)
        if request.action == "retry_failed_stage":
            return self.retry_failed_stage(episode, request)
        if request.action == "approve_stage":
            return self.approve_stage(episode, request)
        if request.action == "reject_stage":
            return self.reject_stage(episode, request)
        if request.action == "continue_after_manual_edit":
            return self.continue_after_manual_edit(episode, request)
        if request.action == "complete":
            return self.complete(episode, request)
        raise ValueError(f"unknown workflow action {request.action}")

    def begin_run(
        self,
        episode: Episode,
        user_id: str | None = None,
    ) -> Episode:
        self.ensure_can_start(episode)
        control = self._control(episode)
        existing_run = control.get("run")
        if (
            isinstance(existing_run, dict)
            and existing_run.get("state") == "running"
            and episode.status not in self.terminal_statuses
        ):
            raise ValueError("episode workflow run is already active")
        now = datetime.now(UTC).isoformat()
        run_sequence = int(control.get("run_sequence") or 0) + 1
        control["run_sequence"] = run_sequence
        run_id = str(uuid4())
        control["run"] = {
            "schema_version": "production_workflow_run.v1",
            "run_id": run_id,
            "run_sequence": run_sequence,
            "state": "running",
            "started_at": now,
            "started_by": user_id or "system",
            "current_stage": episode.status.value,
            "external_temporal_policy": self.external_temporal_policy,
            "external_temporal": self._temporal_summary(),
            "stage_plan": self._updated_stage_plan(
                self._stage_plan(),
                episode.status.value,
            ),
            "stage_history": [
                {
                    "stage": episode.status.value,
                    "entered_at": now,
                    "source": "workflow_run_started",
                }
            ],
            "signals": [],
        }
        control = self._append_workflow_event(
            control,
            "workflow.run.started",
            {
                "recorded_at": now,
                "run_id": run_id,
                "run_sequence": run_sequence,
                "state": "running",
                "stage": episode.status.value,
                "source": "workflow_run_started",
                "actor": user_id or "system",
            },
        )
        control = self._append_temporal_signal_log(
            episode,
            control,
            {
                "signal_id": str(uuid4()),
                "signal_type": "start",
                "received_at": now,
                "stage": episode.status.value,
                "actor": user_id or "system",
                "comment": None,
            },
        )
        episode.workflow_control = control
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.run.started",
                actor=user_id or "system",
                details={
                    "run_id": control["run"]["run_id"],
                    "run_sequence": run_sequence,
                    "current_stage": episode.status.value,
                },
            )
        )
        return self._touch(episode)

    def pause(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        if episode.status in self.terminal_statuses:
            raise ValueError("terminal episodes cannot be paused")
        control = self._control(episode)
        if control.get("paused") is True:
            raise ValueError("episode workflow is already paused")
        now = datetime.now(UTC).isoformat()
        control.update(
            {
                "paused": True,
                "paused_at": now,
                "paused_by": request.user_id or "system",
                "paused_stage": episode.status.value,
                "pause_comment": request.comment,
            }
        )
        control.setdefault("pause_count", 0)
        control["pause_count"] = int(control["pause_count"]) + 1
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="pause",
            stage=episode.status.value,
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.paused",
            request,
            {"paused_stage": episode.status.value},
        )
        return self._touch(episode)

    def resume(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        control = self._control(episode)
        if control.get("paused") is not True:
            raise ValueError("episode workflow is not paused")
        if episode.status == EpisodeStatus.cancelled:
            raise ValueError("cancelled episodes cannot be resumed")
        control.update(
            {
                "paused": False,
                "resumed_at": datetime.now(UTC).isoformat(),
                "resumed_by": request.user_id or "system",
                "resume_comment": request.comment,
            }
        )
        control.setdefault("resume_count", 0)
        control["resume_count"] = int(control["resume_count"]) + 1
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="resume",
            stage=episode.status.value,
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.resumed",
            request,
            {"resumed_stage": episode.status.value},
        )
        return self._touch(episode)

    def cancel(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        if episode.status == EpisodeStatus.cancelled:
            raise ValueError("episode workflow is already cancelled")
        if episode.status == EpisodeStatus.completed:
            raise ValueError("completed episodes cannot be cancelled")
        control = self._control(episode)
        previous_stage = episode.status.value
        control.update(
            {
                "paused": False,
                "cancelled": True,
                "cancelled_at": datetime.now(UTC).isoformat(),
                "cancelled_by": request.user_id or "system",
                "cancelled_from_stage": previous_stage,
                "cancel_comment": request.comment,
            }
        )
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="cancel",
            stage=previous_stage,
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        run = control.get("run")
        if isinstance(run, dict):
            run["state"] = "cancelled"
            run["current_stage"] = EpisodeStatus.cancelled.value
            run["completed_at"] = control["cancelled_at"]
            run["completion_reason"] = "cancelled"
            control["run"] = run
            control = self._append_workflow_event(
                control,
                "workflow.run.completed",
                {
                    "recorded_at": control["cancelled_at"],
                    "state": "cancelled",
                    "stage": EpisodeStatus.cancelled.value,
                    "completion_reason": "cancelled",
                },
            )
        episode.workflow_control = control
        episode.status = EpisodeStatus.cancelled
        self._append_audit(
            episode,
            "workflow.cancelled",
            request,
            {"cancelled_from_stage": previous_stage},
        )
        self._append_audit(
            episode,
            "workflow.stage.changed",
            request,
            {"stage": EpisodeStatus.cancelled.value, "previous_stage": previous_stage},
        )
        return self._touch(episode)

    def stop_run(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        if episode.status in self.terminal_statuses:
            raise ValueError("terminal episode workflow runs cannot be stopped")
        control = self._control(episode)
        run = control.get("run")
        if not isinstance(run, dict) or run.get("state") != "running":
            raise ValueError("episode workflow run is not active")
        now = datetime.now(UTC).isoformat()
        previous_stage = episode.status.value
        control.update(
            {
                "paused": False,
                "cancelled": False,
                "run_stopped_at": now,
                "run_stopped_by": request.user_id or "system",
                "run_stopped_stage": previous_stage,
                "run_stop_comment": request.comment,
            }
        )
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="stop_run",
            stage=previous_stage,
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        run["state"] = "stopped"
        run["current_stage"] = previous_stage
        run["completed_at"] = now
        run["completion_reason"] = "stopped_by_operator"
        run["stopped_by"] = request.user_id or "system"
        run["stop_comment"] = request.comment
        control["run"] = run
        control = self._append_workflow_event(
            control,
            "workflow.run.stopped",
            {
                "recorded_at": now,
                "state": "stopped",
                "stage": previous_stage,
                "completion_reason": "stopped_by_operator",
                "actor": request.user_id or "system",
            },
        )
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.run.stopped",
            request,
            {"stopped_stage": previous_stage},
        )
        return self._touch(episode)

    def retry_failed_stage(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        control = self._control(episode)
        if episode.status != EpisodeStatus.failed and not self._has_failed_assets(episode):
            raise ValueError("retry is only available for failed workflow state or failed assets")
        previous_stage = episode.status.value
        retry_stage = str(
            control.get("failed_stage")
            or control.get("cancelled_from_stage")
            or EpisodeStatus.ready.value
        )
        try:
            episode.status = EpisodeStatus(retry_stage)
        except ValueError:
            episode.status = EpisodeStatus.ready
        control.update(
            {
                "paused": False,
                "cancelled": False,
                "retry_requested_at": datetime.now(UTC).isoformat(),
                "retry_requested_by": request.user_id or "system",
                "retry_from_stage": previous_stage,
                "retry_target_stage": episode.status.value,
                "retry_comment": request.comment,
                "failure_reason": None,
            }
        )
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="retry_failed_stage",
            stage=episode.status.value,
            extra={"previous_stage": previous_stage},
        )
        control = self._resolve_stage_retries(
            control,
            target_stage=episode.status.value,
            resolution="operator_retried",
            resolved_at=signal["received_at"],
            actor=request.user_id or "system",
            signal_id=signal["signal_id"],
        )
        control = self._reopen_run_after_operator_action(
            control,
            stage=episode.status.value,
            source="workflow_retry_requested",
            recorded_at=signal["received_at"],
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        control.setdefault("retry_count", 0)
        control["retry_count"] = int(control["retry_count"]) + 1
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.retry_requested",
            request,
            {
                "previous_stage": previous_stage,
                "target_stage": episode.status.value,
                "retry_count": control["retry_count"],
            },
        )
        self._append_audit(
            episode,
            "workflow.stage.changed",
            request,
            {"stage": episode.status.value, "previous_stage": previous_stage},
        )
        return self._touch(episode)

    def retry_due_stage(
        self,
        episode: Episode,
        user_id: str = "workflow-worker",
        now: datetime | None = None,
    ) -> dict | None:
        control = self._control(episode)
        if control.get("paused") is True or control.get("cancelled") is True:
            return None
        run = control.get("run")
        if not isinstance(run, dict) or run.get("state") != "running":
            return None
        retry = self._next_due_stage_retry(control, now or datetime.now(UTC))
        if retry is None:
            return None
        previous_stage = episode.status.value
        target_stage = str(retry.get("target_stage") or EpisodeStatus.ready.value)
        try:
            episode.status = EpisodeStatus(target_stage)
        except ValueError:
            episode.status = EpisodeStatus.ready
            target_stage = episode.status.value
        recorded_at = (now or datetime.now(UTC)).isoformat()
        signal_id = str(uuid4())
        control.update(
            {
                "paused": False,
                "cancelled": False,
                "automatic_retry_requested_at": recorded_at,
                "automatic_retry_requested_by": user_id,
                "automatic_retry_from_stage": previous_stage,
                "automatic_retry_target_stage": target_stage,
                "automatic_retry_id": retry.get("retry_id"),
                "failure_reason": None,
            }
        )
        control = self._resolve_specific_stage_retry(
            control,
            retry_id=str(retry.get("retry_id") or ""),
            target_stage=target_stage,
            resolution="automatic_retried",
            resolved_at=recorded_at,
            actor=user_id,
            signal_id=signal_id,
        )
        control = self._reopen_run_after_operator_action(
            control,
            stage=target_stage,
            source="workflow_automatic_retry",
            recorded_at=recorded_at,
        )
        control.setdefault("automatic_retry_count", 0)
        control["automatic_retry_count"] = int(control["automatic_retry_count"]) + 1
        control = self._append_workflow_event(
            control,
            "workflow.stage_retry.automatic_retry_requested",
            {
                "recorded_at": recorded_at,
                "retry_id": retry.get("retry_id"),
                "previous_stage": previous_stage,
                "target_stage": target_stage,
                "actor": user_id,
                "signal_id": signal_id,
                "attempt_number": retry.get("attempt_number"),
            },
        )
        episode.workflow_control = control
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.retry_automatic_requested",
                actor=user_id,
                details={
                    "retry_id": retry.get("retry_id"),
                    "previous_stage": previous_stage,
                    "target_stage": target_stage,
                    "attempt_number": retry.get("attempt_number"),
                },
            )
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=user_id,
                details={"stage": target_stage, "previous_stage": previous_stage},
            )
        )
        self._touch(episode)
        return {
            "schema_version": "workflow_automatic_stage_retry.v1",
            "episode_id": str(episode.id),
            "retry_id": str(retry.get("retry_id") or ""),
            "previous_stage": previous_stage,
            "target_stage": target_stage,
            "attempt_number": int(retry.get("attempt_number") or 0),
            "resolved_at": recorded_at,
            "actor": user_id,
        }

    def acknowledge_stage_retry(
        self,
        episode: Episode,
        retry_id: str,
        request: WorkflowRetryResolutionRequest,
    ) -> Episode:
        control = self._control(episode)
        retry_queue = control.get("stage_retry_queue", [])
        if not isinstance(retry_queue, list):
            raise ValueError("workflow retry queue is unavailable")
        retry = next(
            (
                item
                for item in retry_queue
                if isinstance(item, dict) and str(item.get("retry_id") or "") == retry_id
            ),
            None,
        )
        if retry is None:
            raise ValueError("workflow retry not found")
        previous_status = str(retry.get("status") or "")
        if previous_status not in {"scheduled", "exhausted"}:
            raise ValueError("workflow retry is already resolved")
        target_stage = str(retry.get("target_stage") or "")
        if not target_stage:
            raise ValueError("workflow retry target stage is missing")

        actor = request.user_id or "system"
        recorded_at = datetime.now(UTC).isoformat()
        signal_id = str(uuid4())
        control = self._resolve_specific_stage_retry(
            control,
            retry_id=retry_id,
            target_stage=target_stage,
            resolution="operator_acknowledged",
            resolved_at=recorded_at,
            actor=actor,
            signal_id=signal_id,
            eligible_statuses={"scheduled", "exhausted"},
            comment=request.comment,
        )
        control["last_stage_retry_acknowledgement"] = {
            "schema_version": "workflow_stage_retry_acknowledgement.v1",
            "retry_id": retry_id,
            "target_stage": target_stage,
            "previous_status": previous_status,
            "acknowledged_at": recorded_at,
            "acknowledged_by": actor,
            "comment_present": bool(request.comment),
        }
        control = self._append_workflow_event(
            control,
            "workflow.stage_retry.operator_acknowledged",
            {
                "recorded_at": recorded_at,
                "retry_id": retry_id,
                "target_stage": target_stage,
                "previous_status": previous_status,
                "actor": actor,
                "signal_id": signal_id,
                "comment_present": bool(request.comment),
            },
        )
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.stage_retry.acknowledged",
            WorkflowActionRequest(
                action="complete",
                user_id=request.user_id,
                comment=request.comment,
            ),
            {
                "retry_id": retry_id,
                "target_stage": target_stage,
                "previous_status": previous_status,
                "resolution": "operator_acknowledged",
            },
        )
        return self._touch(episode)

    def continue_after_manual_edit(
        self,
        episode: Episode,
        request: WorkflowActionRequest,
    ) -> Episode:
        if episode.status in self.terminal_statuses:
            raise ValueError("terminal episodes cannot continue after manual edit")
        control = self._control(episode)
        previous_stage = episode.status.value
        if episode.status == EpisodeStatus.failed:
            target_stage = str(
                control.get("failed_stage")
                or control.get("last_rejected_stage")
                or control.get("paused_stage")
                or EpisodeStatus.ready.value
            )
        elif control.get("paused") is True:
            target_stage = str(control.get("paused_stage") or previous_stage)
        else:
            # A repaired episode may already be READY after rebuilding its
            # timeline. Historical failure metadata must not move it back into
            # an earlier production stage and disable the next operator action.
            target_stage = previous_stage
        try:
            episode.status = EpisodeStatus(target_stage)
        except ValueError:
            episode.status = EpisodeStatus.ready
        continued_at = datetime.now(UTC).isoformat()
        manual_edit_evidence = self._manual_edit_evidence(
            episode,
            since=control.get("failed_at"),
        )
        control.update(
            {
                "paused": False,
                "cancelled": False,
                "manual_edit_continued_at": continued_at,
                "manual_edit_continued_by": request.user_id or "system",
                "manual_edit_previous_stage": previous_stage,
                "manual_edit_target_stage": episode.status.value,
                "manual_edit_comment": request.comment,
                "manual_edit_evidence": manual_edit_evidence,
                "failure_reason": None,
            }
        )
        control.pop("retry_exhausted", None)
        control.pop("retry_exhausted_stage", None)
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="continue_after_manual_edit",
            stage=episode.status.value,
            extra={
                "previous_stage": previous_stage,
                "manual_edit_evidence": manual_edit_evidence,
            },
        )
        control = self._resolve_stage_retries(
            control,
            target_stage=episode.status.value,
            resolution="manual_edit_resolved",
            resolved_at=signal["received_at"],
            actor=request.user_id or "system",
            signal_id=signal["signal_id"],
        )
        control = self._reopen_run_after_operator_action(
            control,
            stage=episode.status.value,
            source="manual_edit_continued",
            recorded_at=signal["received_at"],
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        control.setdefault("manual_edit_continue_count", 0)
        control["manual_edit_continue_count"] = int(control["manual_edit_continue_count"]) + 1
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.manual_edit.continued",
            request,
            {
                "previous_stage": previous_stage,
                "target_stage": episode.status.value,
                "continue_count": control["manual_edit_continue_count"],
                "manual_edit_evidence": manual_edit_evidence,
            },
        )
        if previous_stage != episode.status.value:
            self._append_audit(
                episode,
                "workflow.stage.changed",
                request,
                {"stage": episode.status.value, "previous_stage": previous_stage},
            )
        return self._touch(episode)

    def _manual_edit_evidence(self, episode: Episode, since: object = None) -> dict:
        manual_event_types = {
            "asset.replaced",
            "timeline.asset.edited",
            "transcript.turn.regenerated",
            "transcript.turn.excluded",
        }
        since_at = self._parse_datetime(since)
        events = [
            event
            for event in episode.audit_events
            if event.event_type in manual_event_types
            and (since_at is None or event.created_at >= since_at)
        ]
        entries = [
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "actor": event.actor,
                "created_at": event.created_at.isoformat(),
                "details": self._manual_edit_event_details(event.details),
            }
            for event in events
        ]
        evidence = {
            "schema_version": "manual_edit_evidence.v1",
            "since": since_at.isoformat() if since_at is not None else None,
            "event_count": len(entries),
            "by_event_type": {
                event_type: sum(1 for entry in entries if entry["event_type"] == event_type)
                for event_type in sorted(manual_event_types)
                if any(entry["event_type"] == event_type for entry in entries)
            },
            "events": entries,
        }
        evidence["evidence_checksum"] = self._stable_checksum(evidence)
        return evidence

    def _manual_edit_event_details(self, details: dict) -> dict:
        allowed = {
            "asset_id",
            "original_asset_id",
            "replacement_asset_id",
            "asset_type",
            "source_entity_type",
            "source_entity_id",
            "timeline_asset_updates",
            "transcript_version_id",
            "previous_asset_id",
            "segment_count",
            "duration_ms",
            "checksum",
            "turn_id",
            "transcript_turn_id",
            "source_discussion_turn_ids",
            "status",
            "comment",
        }
        if not isinstance(details, dict):
            return {}
        return {key: value for key, value in details.items() if key in allowed}

    def _parse_datetime(self, value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def approve_stage(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        if episode.status in self.terminal_statuses:
            raise ValueError("terminal episodes cannot receive stage approvals")
        control = self._control(episode)
        stage = episode.status.value
        now = datetime.now(UTC).isoformat()
        entry = {
            "schema_version": "workflow_stage_decision.v1",
            "decision_id": str(uuid4()),
            "decision": "approved",
            "stage": stage,
            "decided_at": now,
            "decided_by": request.user_id or "system",
            "comment": request.comment,
        }
        decisions = list(control.get("stage_decisions", []))
        decisions.append(entry)
        control["stage_decisions"] = decisions
        control["last_stage_decision"] = entry
        control["last_approved_stage"] = stage
        control.setdefault("stage_approval_count", 0)
        control["stage_approval_count"] = int(control["stage_approval_count"]) + 1
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="approve_stage",
            stage=stage,
            extra={"decision_id": entry["decision_id"], "decision": "approved"},
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        episode.workflow_control = control
        self._append_audit(
            episode,
            "workflow.stage.approved",
            request,
            {"stage": stage, "decision_id": entry["decision_id"]},
        )
        return self._touch(episode)

    def reject_stage(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        if episode.status in self.terminal_statuses:
            raise ValueError("terminal episodes cannot receive stage rejections")
        control = self._control(episode)
        rejected_stage = episode.status.value
        now = datetime.now(UTC).isoformat()
        entry = {
            "schema_version": "workflow_stage_decision.v1",
            "decision_id": str(uuid4()),
            "decision": "rejected",
            "stage": rejected_stage,
            "decided_at": now,
            "decided_by": request.user_id or "system",
            "comment": request.comment,
        }
        decisions = list(control.get("stage_decisions", []))
        decisions.append(entry)
        control["stage_decisions"] = decisions
        control["last_stage_decision"] = entry
        control["last_rejected_stage"] = rejected_stage
        control["failed_stage"] = rejected_stage
        control["failed_at"] = now
        control["failed_by"] = request.user_id or "system"
        control["failure_reason"] = "stage rejected by workflow control"
        control.setdefault("stage_rejection_count", 0)
        control["stage_rejection_count"] = int(control["stage_rejection_count"]) + 1
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="reject_stage",
            stage=rejected_stage,
            extra={"decision_id": entry["decision_id"], "decision": "rejected"},
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        run = control.get("run")
        if isinstance(run, dict):
            run["state"] = "failed"
            run["current_stage"] = EpisodeStatus.failed.value
            run["completed_at"] = now
            run["completion_reason"] = "stage_rejected"
            run["failed_stage"] = rejected_stage
            control["run"] = run
            control = self._append_workflow_event(
                control,
                "workflow.run.completed",
                {
                    "recorded_at": now,
                    "state": "failed",
                    "stage": EpisodeStatus.failed.value,
                    "completion_reason": "stage_rejected",
                    "failed_stage": rejected_stage,
                },
            )
        previous_stage = rejected_stage
        episode.workflow_control = control
        episode.status = EpisodeStatus.failed
        self._append_audit(
            episode,
            "workflow.stage.rejected",
            request,
            {"stage": rejected_stage, "decision_id": entry["decision_id"]},
        )
        self._append_audit(
            episode,
            "workflow.stage.changed",
            request,
            {"stage": EpisodeStatus.failed.value, "previous_stage": previous_stage},
        )
        return self._touch(episode)

    def complete(self, episode: Episode, request: WorkflowActionRequest) -> Episode:
        if episode.status == EpisodeStatus.cancelled:
            raise ValueError("cancelled episodes cannot be completed")
        if episode.status == EpisodeStatus.completed:
            raise ValueError("episode workflow is already completed")
        if self._control(episode).get("paused") is True:
            raise ValueError("paused episodes cannot be completed")
        readiness = self.ensure_completion_ready(episode)
        previous_stage = episode.status.value
        control = self._control(episode)
        control.update(
            {
                "completion_requested_at": datetime.now(UTC).isoformat(),
                "completion_requested_by": request.user_id or "system",
                "completion_comment": request.comment,
            }
        )
        control, signal = self._append_workflow_signal(
            control,
            request,
            signal_type="complete",
            stage=EpisodeStatus.completed.value,
            extra={
                "previous_stage": previous_stage,
                "completion_gate_status": readiness["status"],
            },
        )
        control = self._append_temporal_signal_log(episode, control, signal)
        episode.workflow_control = control
        episode.status = EpisodeStatus.completed
        self.record_stage(episode, EpisodeStatus.completed, "operator_completed")
        self._append_audit(
            episode,
            "workflow.completed",
            request,
            {
                "previous_stage": previous_stage,
                "completion_gate_status": readiness["status"],
                "final_render_asset_id": readiness["final_render_asset_id"],
                "export_package_asset_id": readiness["export_package_asset_id"],
                "production_manifest_asset_id": readiness["production_manifest_asset_id"],
            },
        )
        self._append_audit(
            episode,
            "workflow.stage.changed",
            request,
            {"stage": EpisodeStatus.completed.value, "previous_stage": previous_stage},
        )
        return self._touch(episode)

    def ensure_can_start(self, episode: Episode) -> None:
        control = self._control(episode)
        if control.get("paused") is True:
            raise ValueError("episode workflow is paused")
        if episode.status == EpisodeStatus.cancelled or control.get("cancelled") is True:
            raise ValueError("episode workflow is cancelled")

    def completion_readiness(self, episode: Episode) -> dict:
        transcript = self._approved_canonical_transcript(episode)
        latest_transcript = self._canonical_or_latest_broadcast_transcript(episode)
        discussion_qc = self._latest_discussion_quality_result(episode)
        media_handoff = self._transcript_media_handoff(episode, transcript)
        visual_source_summary = self._visual_source_summary(episode, transcript)
        character_config_handoff = self._character_config_handoff(episode, transcript)
        localized_output_readiness = self._localized_output_readiness(episode)
        evidence_pack = self._latest_completed_evidence_pack_asset(episode)
        evidence_pack_qc = self._latest_evidence_pack_quality_result(episode, evidence_pack)
        research_approval = self._latest_approval(episode, "research_review")
        claim_qc = self._latest_claim_quality_result(episode, transcript, evidence_pack)
        audio_qc = self._latest_transcript_quality_result(
            episode,
            transcript,
            "audio_media_integrity",
        )
        visual_qc = self._latest_transcript_quality_result(
            episode,
            transcript,
            "visual_media_integrity",
        )
        subtitle_qc = self._latest_subtitle_quality_result(
            episode,
            media_handoff["subtitle_asset_id"],
        )
        timeline_qc = self._latest_timeline_quality_result(
            episode,
            media_handoff["timeline_asset_id"],
        )
        preview_render = self._latest_completed_preview_render_asset(
            episode,
            media_handoff["timeline_asset_id"],
        )
        preview_render_qc = self._latest_preview_render_quality_result(
            episode,
            preview_render,
        )
        final_render = self._latest_completed_final_render_asset(episode)
        final_render_qc = self._latest_final_render_quality_result(episode, final_render)
        preview_render_source_freshness = self._render_source_asset_freshness(
            preview_render_qc,
        )
        final_render_source_freshness = self._render_source_asset_freshness(
            final_render_qc,
        )
        thumbnail = self._latest_completed_thumbnail_asset(episode, final_render)
        thumbnail_qc = self._latest_thumbnail_quality_result(episode, thumbnail)
        package = self._latest_completed_export_package_asset(episode, final_render)
        manifest = self._latest_completed_production_manifest_asset(episode, package)
        package_qc = self._latest_package_quality_result(episode, package)
        publish_job = self._latest_publish_job(episode, package)
        publish_qc = self._latest_publish_quality_result(episode, publish_job)
        final_render_timeline_linked = self._final_render_linked_to_timeline(
            final_render,
            media_handoff["timeline_asset_id"],
        )
        manifest_validity = (
            self._production_manifest_asset_valid(
                manifest,
                package,
                thumbnail,
                media_handoff["subtitle_asset_id"],
            )
            if manifest is not None and package is not None
            else {"valid": False, "reason": "production manifest is missing"}
        )
        manifest_publish_evidence = self._production_manifest_publish_evidence(
            manifest,
            publish_job,
            publish_qc,
        )
        failed_assets = self._unresolved_failed_assets(episode)
        resolved_failed_assets = self._resolved_failed_assets(episode)
        blocking_failed_assets = [
            asset for asset in failed_assets if asset["blocks_completion"] is True
        ]
        nonblocking_failed_assets = [
            asset for asset in failed_assets if asset["blocks_completion"] is False
        ]
        failing_qc = self._latest_failing_quality_results(episode)
        blocking_qc = [result for result in failing_qc if result["blocks_completion"] is True]
        nonblocking_qc = [result for result in failing_qc if result["blocks_completion"] is False]
        final_render_approved = final_render is not None and any(
            approval.target_type == "render_asset"
            and approval.target_id == str(final_render.id)
            and approval.decision == "approved"
            for approval in episode.approvals
        )
        preview_render_approved = preview_render is not None and any(
            approval.stage == "preview_render_review"
            and approval.target_type == "render_asset"
            and approval.target_id == str(preview_render.id)
            and approval.decision == "approved"
            for approval in episode.approvals
        )
        audio_qc_required = (
            transcript is not None
            and media_handoff["playable_turn_count"] > 0
            and not media_handoff["missing_audio_turn_ids"]
        )
        visual_qc_required = (
            transcript is not None
            and media_handoff["playable_turn_count"] > 0
            and not media_handoff["missing_primary_visual_turn_ids"]
        )
        subtitle_qc_required = (
            transcript is not None and media_handoff["subtitle_asset_id"] is not None
        )
        timeline_qc_required = (
            transcript is not None
            and media_handoff["timeline_asset_id"] is not None
            and not media_handoff["missing_timeline_segment_turn_ids"]
            and not media_handoff["missing_reaction_loop_turn_ids"]
            and not media_handoff["missing_studio_scene_turn_ids"]
        )
        preview_render_required = timeline_qc_required
        production_target = getattr(
            episode.definition.workflow,
            "production_target",
            "native_visual",
        )
        failed_checks: list[str] = []
        if latest_transcript is None:
            failed_checks.append("canonical_transcript_missing")
        elif latest_transcript.status != "approved":
            failed_checks.append("canonical_transcript_not_approved")
        if discussion_qc is None and episode.discussion_session is not None:
            failed_checks.append("discussion_structure_qc_missing")
        elif discussion_qc is not None and (
            (discussion_qc.status == "fail" or discussion_qc.severity == QualitySeverity.fail)
            and self._quality_result_blocks_completion(episode, discussion_qc)
        ):
            failed_checks.append("discussion_structure_qc_failing")
        if transcript is not None:
            if media_handoff["playable_turn_count"] == 0:
                failed_checks.append("playable_turns_missing")
            if character_config_handoff["unknown_speaker_participant_ids"]:
                failed_checks.append("character_profile_missing")
            if character_config_handoff["missing_model_participant_ids"]:
                failed_checks.append("character_model_missing")
            if media_handoff["stale_model_turn_ids"]:
                failed_checks.append("character_model_turn_stale")
            if character_config_handoff["missing_voice_participant_ids"]:
                failed_checks.append("character_voice_missing")
            if character_config_handoff["missing_visual_participant_ids"]:
                failed_checks.append("character_visual_missing")
            if media_handoff["missing_audio_turn_ids"]:
                failed_checks.append("completed_audio_missing")
            if media_handoff["stale_voice_asset_turn_ids"]:
                failed_checks.append("character_voice_asset_stale")
            if media_handoff["missing_primary_visual_turn_ids"]:
                failed_checks.append("completed_character_visual_missing")
            if (
                production_target == "native_visual"
                and visual_source_summary["playable_turn_count"] > 0
                and visual_source_summary["native_visual_complete"] is False
                and not media_handoff["missing_primary_visual_turn_ids"]
            ):
                failed_checks.append("native_primary_visuals_missing")
            if media_handoff["stale_visual_asset_turn_ids"]:
                failed_checks.append("character_visual_asset_stale")
            if media_handoff["subtitle_asset_id"] is None:
                failed_checks.append("subtitle_asset_missing")
            if media_handoff["timeline_asset_id"] is None:
                failed_checks.append("timeline_asset_missing")
            elif media_handoff["missing_timeline_segment_turn_ids"]:
                failed_checks.append("timeline_segments_missing")
            if media_handoff["missing_reaction_loop_turn_ids"]:
                failed_checks.append("shot_planned_reaction_loop_missing")
            if media_handoff["missing_studio_scene_turn_ids"]:
                failed_checks.append("shot_planned_studio_scene_missing")
        if localized_output_readiness["missing_languages"]:
            failed_checks.append("localized_output_missing")
        if localized_output_readiness["not_approved_languages"]:
            failed_checks.append("localized_output_not_approved")
        if localized_output_readiness["qc_missing_languages"]:
            failed_checks.append("localized_output_qc_missing")
        if localized_output_readiness["qc_failing_languages"]:
            failed_checks.append("localized_output_qc_failing")
        if audio_qc_required and audio_qc is None:
            failed_checks.append("audio_qc_missing")
        if (
            audio_qc is not None
            and (audio_qc.status == "fail" or audio_qc.severity == QualitySeverity.fail)
            and self._quality_result_blocks_completion(episode, audio_qc)
        ):
            failed_checks.append("audio_qc_failing")
        if visual_qc_required and visual_qc is None:
            failed_checks.append("visual_qc_missing")
        if (
            visual_qc is not None
            and (visual_qc.status == "fail" or visual_qc.severity == QualitySeverity.fail)
            and self._quality_result_blocks_completion(episode, visual_qc)
        ):
            failed_checks.append("visual_qc_failing")
        if subtitle_qc_required and subtitle_qc is None:
            failed_checks.append("subtitle_qc_missing")
        if (
            subtitle_qc is not None
            and (subtitle_qc.status == "fail" or subtitle_qc.severity == QualitySeverity.fail)
            and self._quality_result_blocks_completion(episode, subtitle_qc)
        ):
            failed_checks.append("subtitle_qc_failing")
        if timeline_qc_required and timeline_qc is None:
            failed_checks.append("timeline_qc_missing")
        if (
            timeline_qc is not None
            and (timeline_qc.status == "fail" or timeline_qc.severity == QualitySeverity.fail)
            and self._quality_result_blocks_completion(episode, timeline_qc)
        ):
            failed_checks.append("timeline_qc_failing")
        if preview_render_required and preview_render is None:
            failed_checks.append("preview_render_missing")
        if preview_render is not None and preview_render_qc is None:
            failed_checks.append("preview_render_qc_missing")
        if preview_render_qc is not None and (
            preview_render_qc.status == "fail"
            or preview_render_qc.severity == QualitySeverity.fail
        ):
            failed_checks.append("preview_render_qc_failing")
        if preview_render_source_freshness["fresh"] is False:
            failed_checks.append("preview_render_source_assets_stale")
        if preview_render is not None and not preview_render_approved:
            failed_checks.append("preview_render_approval_missing")
        if episode.definition.research.enabled and evidence_pack is None:
            failed_checks.append("research_evidence_pack_missing")
        if (
            episode.definition.research.enabled
            and evidence_pack is not None
            and evidence_pack_qc is None
        ):
            failed_checks.append("research_evidence_pack_qc_missing")
        if evidence_pack_qc is not None and (
            evidence_pack_qc.status == "fail"
            or evidence_pack_qc.severity == QualitySeverity.fail
        ):
            failed_checks.append("research_evidence_pack_qc_failing")
        if episode.definition.research.enabled and episode.definition.research.approval_required:
            if research_approval is None or research_approval.decision == "pending":
                failed_checks.append("research_approval_missing")
            elif research_approval.decision == "rejected":
                failed_checks.append("research_approval_rejected")
        if (
            episode.definition.research.enabled
            and transcript is not None
            and evidence_pack is not None
        ):
            if claim_qc is None:
                failed_checks.append("claim_qc_missing")
            elif (
                (claim_qc.status == "fail" or claim_qc.severity == QualitySeverity.fail)
                and self._quality_result_blocks_completion(episode, claim_qc)
            ):
                failed_checks.append("claim_qc_failing")
        if final_render is None:
            failed_checks.append("completed_final_render_missing")
        if final_render is not None and final_render_timeline_linked is False:
            failed_checks.append("final_render_timeline_mismatch")
        if final_render is not None and final_render_qc is None:
            failed_checks.append("final_render_qc_missing")
        if final_render_qc is not None and (
            final_render_qc.status == "fail"
            or final_render_qc.severity == QualitySeverity.fail
        ):
            failed_checks.append("final_render_qc_failing")
        if final_render_source_freshness["fresh"] is False:
            failed_checks.append("final_render_source_assets_stale")
        if final_render is not None and not final_render_approved:
            failed_checks.append("final_render_approval_missing")
        if final_render is not None and thumbnail is None:
            failed_checks.append("thumbnail_missing")
        if thumbnail is not None and thumbnail_qc is None:
            failed_checks.append("thumbnail_qc_missing")
        if thumbnail_qc is not None and (
            thumbnail_qc.status == "fail" or thumbnail_qc.severity == QualitySeverity.fail
        ):
            failed_checks.append("thumbnail_qc_failing")
        if package is None:
            failed_checks.append("completed_export_package_missing")
        if package is not None and package_qc is None:
            failed_checks.append("export_package_qc_missing")
        if package_qc is not None and (
            package_qc.status == "fail" or package_qc.severity == QualitySeverity.fail
        ):
            failed_checks.append("export_package_qc_failing")
        if (
            package is not None
            and thumbnail is not None
            and not self._export_package_includes_thumbnail(package, thumbnail)
        ):
            failed_checks.append("export_package_thumbnail_missing")
        if (
            package is not None
            and media_handoff["subtitle_asset_id"] is not None
            and not self._export_package_includes_subtitles(package)
        ):
            failed_checks.append("export_package_subtitles_missing")
        if manifest is None:
            failed_checks.append("completed_production_manifest_missing")
        if manifest is not None and manifest_validity["valid"] is False:
            failed_checks.append("production_manifest_invalid")
        if package is not None and publish_job is None:
            failed_checks.append("publish_job_missing")
        if publish_job is not None and publish_job.status != "completed":
            failed_checks.append("publish_job_not_completed")
        if publish_job is not None and publish_job.status == "completed" and publish_qc is None:
            failed_checks.append("publish_delivery_qc_missing")
        if (
            publish_qc is not None
            and (publish_qc.status == "fail" or publish_qc.severity == QualitySeverity.fail)
            and self._quality_result_blocks_completion(episode, publish_qc)
        ):
            failed_checks.append("publish_delivery_qc_failing")
        if (
            publish_job is not None
            and publish_job.status == "completed"
            and publish_qc is not None
            and not self._quality_result_blocks_completion(episode, publish_qc)
            and manifest_publish_evidence["valid"] is False
        ):
            failed_checks.append("production_manifest_publish_evidence_missing")
        if blocking_failed_assets:
            failed_checks.append("unresolved_failed_assets_present")
        if blocking_qc:
            failed_checks.append("failing_quality_results_present")
        status = "pass" if not failed_checks else "fail"
        target_policy_satisfied = (
            visual_source_summary["playable_turn_count"] > 0
            and visual_source_summary["native_visual_complete"] is True
            if production_target == "native_visual"
            else True
        )
        return {
            "schema_version": "production_completion_readiness.v1",
            "status": status,
            "failed_checks": failed_checks,
            "production_target": production_target,
            "production_target_satisfied": target_policy_satisfied,
            "quality_blocking_policy": self._quality_blocking_policy(episode),
            "canonical_transcript_version_id": str(transcript.id) if transcript else None,
            "canonical_transcript_status": latest_transcript.status if latest_transcript else None,
            "discussion_qc_id": str(discussion_qc.id) if discussion_qc else None,
            "discussion_qc_status": discussion_qc.status if discussion_qc else None,
            "discussion_qc_missing_dimensions": (
                list(discussion_qc.details.get("missing_dimensions", []))
                if discussion_qc is not None
                and isinstance(discussion_qc.details.get("missing_dimensions"), list)
                else []
            ),
            "character_configuration": character_config_handoff,
            "localized_output_readiness": localized_output_readiness,
            "localized_outputs_required": localized_output_readiness["required"],
            "missing_localized_output_languages": localized_output_readiness["missing_languages"],
            "not_approved_localized_output_languages": (
                localized_output_readiness["not_approved_languages"]
            ),
            "localized_output_qc_missing_languages": (
                localized_output_readiness["qc_missing_languages"]
            ),
            "localized_output_qc_failing_languages": (
                localized_output_readiness["qc_failing_languages"]
            ),
            "playable_turn_count": media_handoff["playable_turn_count"],
            "completed_audio_turn_count": media_handoff["completed_audio_turn_count"],
            "completed_primary_visual_turn_count": (
                media_handoff["completed_primary_visual_turn_count"]
            ),
            "visual_source_summary": visual_source_summary,
            "missing_audio_turn_ids": media_handoff["missing_audio_turn_ids"],
            "missing_primary_visual_turn_ids": media_handoff["missing_primary_visual_turn_ids"],
            "stale_model_turn_ids": media_handoff["stale_model_turn_ids"],
            "stale_voice_asset_turn_ids": media_handoff["stale_voice_asset_turn_ids"],
            "stale_visual_asset_turn_ids": media_handoff["stale_visual_asset_turn_ids"],
            "expected_reaction_loop_segment_count": (
                media_handoff["expected_reaction_loop_segment_count"]
            ),
            "linked_reaction_loop_segment_count": (
                media_handoff["linked_reaction_loop_segment_count"]
            ),
            "missing_reaction_loop_turn_ids": media_handoff["missing_reaction_loop_turn_ids"],
            "expected_studio_scene_segment_count": (
                media_handoff["expected_studio_scene_segment_count"]
            ),
            "linked_studio_scene_segment_count": (
                media_handoff["linked_studio_scene_segment_count"]
            ),
            "missing_studio_scene_turn_ids": media_handoff["missing_studio_scene_turn_ids"],
            "subtitle_asset_id": media_handoff["subtitle_asset_id"],
            "timeline_asset_id": media_handoff["timeline_asset_id"],
            "missing_timeline_segment_turn_ids": (
                media_handoff["missing_timeline_segment_turn_ids"]
            ),
            "audio_qc_id": str(audio_qc.id) if audio_qc else None,
            "audio_qc_status": audio_qc.status if audio_qc else None,
            "visual_qc_id": str(visual_qc.id) if visual_qc else None,
            "visual_qc_status": visual_qc.status if visual_qc else None,
            "subtitle_qc_id": str(subtitle_qc.id) if subtitle_qc else None,
            "subtitle_qc_status": subtitle_qc.status if subtitle_qc else None,
            "timeline_qc_id": str(timeline_qc.id) if timeline_qc else None,
            "timeline_qc_status": timeline_qc.status if timeline_qc else None,
            "preview_render_asset_id": str(preview_render.id) if preview_render else None,
            "preview_render_qc_id": str(preview_render_qc.id) if preview_render_qc else None,
            "preview_render_qc_status": preview_render_qc.status if preview_render_qc else None,
            "preview_render_source_assets_fresh": preview_render_source_freshness["fresh"],
            "preview_render_stale_source_asset_count": (
                preview_render_source_freshness["stale_source_asset_count"]
            ),
            "preview_render_missing_source_asset_count": (
                preview_render_source_freshness["missing_source_asset_count"]
            ),
            "preview_render_approved": preview_render_approved,
            "research_required": episode.definition.research.enabled,
            "evidence_pack_asset_id": str(evidence_pack.id) if evidence_pack else None,
            "evidence_pack_qc_id": str(evidence_pack_qc.id) if evidence_pack_qc else None,
            "evidence_pack_qc_status": evidence_pack_qc.status if evidence_pack_qc else None,
            "research_approval_required": (
                episode.definition.research.enabled
                and episode.definition.research.approval_required
            ),
            "research_approval_id": str(research_approval.id) if research_approval else None,
            "research_approval_status": research_approval.decision if research_approval else None,
            "claim_qc_id": str(claim_qc.id) if claim_qc else None,
            "claim_qc_status": claim_qc.status if claim_qc else None,
            "final_render_asset_id": str(final_render.id) if final_render else None,
            "final_render_timeline_linked": final_render_timeline_linked,
            "final_render_qc_id": str(final_render_qc.id) if final_render_qc else None,
            "final_render_qc_status": final_render_qc.status if final_render_qc else None,
            "final_render_source_assets_fresh": final_render_source_freshness["fresh"],
            "final_render_stale_source_asset_count": (
                final_render_source_freshness["stale_source_asset_count"]
            ),
            "final_render_missing_source_asset_count": (
                final_render_source_freshness["missing_source_asset_count"]
            ),
            "thumbnail_asset_id": str(thumbnail.id) if thumbnail else None,
            "thumbnail_qc_id": str(thumbnail_qc.id) if thumbnail_qc else None,
            "thumbnail_qc_status": thumbnail_qc.status if thumbnail_qc else None,
            "export_package_asset_id": str(package.id) if package else None,
            "export_package_qc_id": str(package_qc.id) if package_qc else None,
            "export_package_qc_status": package_qc.status if package_qc else None,
            "export_package_thumbnail_included": (
                self._export_package_includes_thumbnail(package, thumbnail)
                if package is not None and thumbnail is not None
                else None
            ),
            "export_package_subtitles_included": (
                self._export_package_includes_subtitles(package)
                if package is not None and media_handoff["subtitle_asset_id"] is not None
                else None
            ),
            "production_manifest_asset_id": str(manifest.id) if manifest else None,
            "production_manifest_valid": manifest_validity["valid"],
            "production_manifest_invalid_reason": manifest_validity["reason"],
            "production_manifest_publish_evidence_valid": manifest_publish_evidence["valid"],
            "production_manifest_publish_evidence_reason": manifest_publish_evidence["reason"],
            "final_render_approved": final_render_approved,
            "publish_job_id": str(publish_job.id) if publish_job else None,
            "publish_job_status": publish_job.status if publish_job else None,
            "publish_job_dry_run": publish_job.dry_run if publish_job else None,
            "publish_job_target_id": publish_job.publisher_target_id if publish_job else None,
            "publish_delivery_qc_id": str(publish_qc.id) if publish_qc else None,
            "publish_delivery_qc_status": publish_qc.status if publish_qc else None,
            "unresolved_failed_assets": blocking_failed_assets,
            "nonblocking_unresolved_failed_assets": nonblocking_failed_assets,
            "resolved_failed_assets": resolved_failed_assets,
            "failing_quality_results": blocking_qc,
            "nonblocking_failing_quality_results": nonblocking_qc,
        }

    def _visual_source_summary(self, episode: Episode, transcript) -> dict:
        if transcript is None:
            return {
                "schema_version": "visual_source_summary.v1",
                "playable_turn_count": 0,
                "completed_primary_visual_turn_count": 0,
                "native_primary_visual_turn_count": 0,
                "fallback_primary_visual_turn_count": 0,
                "missing_primary_visual_turn_count": 0,
                "native_visual_complete": False,
                "fallback_visual_used": False,
                "fallback_primary_visual_turn_ids": [],
                "missing_primary_visual_turn_ids": [],
                "sample_fallback_reasons": [],
            }
        playable_turn_ids = [
            str(turn.id) for turn in transcript.turns if turn.status != "excluded"
        ]
        completed_by_turn: dict[str, Asset] = {}
        for asset in episode.assets:
            if (
                asset.asset_type == AssetType.video
                and asset.status == "completed"
                and asset.source_entity_type == "transcript_turn"
                and str(asset.source_entity_id or "") in playable_turn_ids
                and asset.generation_metadata.get("visual_role") == "video_primary"
            ):
                completed_by_turn[str(asset.source_entity_id)] = asset

        fallback_turn_ids = [
            turn_id
            for turn_id in playable_turn_ids
            if (asset := completed_by_turn.get(turn_id)) is not None
            and asset.generation_metadata.get("fallback_visual") is True
        ]
        missing_turn_ids = [
            turn_id for turn_id in playable_turn_ids if turn_id not in completed_by_turn
        ]
        fallback_reasons = []
        for turn_id in fallback_turn_ids:
            reason = completed_by_turn[turn_id].generation_metadata.get("fallback_reason")
            if isinstance(reason, str) and reason and reason not in fallback_reasons:
                fallback_reasons.append(reason)
        native_count = len(completed_by_turn) - len(fallback_turn_ids)
        return {
            "schema_version": "visual_source_summary.v1",
            "playable_turn_count": len(playable_turn_ids),
            "completed_primary_visual_turn_count": len(completed_by_turn),
            "native_primary_visual_turn_count": native_count,
            "fallback_primary_visual_turn_count": len(fallback_turn_ids),
            "missing_primary_visual_turn_count": len(missing_turn_ids),
            "native_visual_complete": (
                bool(playable_turn_ids)
                and not missing_turn_ids
                and len(fallback_turn_ids) == 0
            ),
            "fallback_visual_used": bool(fallback_turn_ids),
            "fallback_primary_visual_turn_ids": fallback_turn_ids[:20],
            "missing_primary_visual_turn_ids": missing_turn_ids[:20],
            "sample_fallback_reasons": fallback_reasons[:3],
        }

    def ensure_completion_ready(self, episode: Episode) -> dict:
        readiness = self.completion_readiness(episode)
        if readiness["status"] != "pass":
            failed = ", ".join(readiness["failed_checks"])
            raise ValueError(f"production cannot be marked completed until gates pass: {failed}")
        return readiness

    def record_stage(
        self,
        episode: Episode,
        stage: EpisodeStatus,
        source: str,
    ) -> None:
        control = self._control(episode)
        existing_run = control.get("run")
        if not isinstance(existing_run, dict):
            return
        readiness = (
            self.ensure_completion_ready(episode) if stage == EpisodeStatus.completed else None
        )
        run = dict(existing_run)
        stage_value = stage.value
        now = datetime.now(UTC).isoformat()
        run["current_stage"] = stage_value
        run["updated_at"] = now
        history = list(run.get("stage_history", []))
        if not history or history[-1].get("stage") != stage_value:
            history.append({"stage": stage_value, "entered_at": now, "source": source})
            control = self._append_workflow_event(
                control,
                "workflow.stage.entered",
                {
                    "recorded_at": now,
                    "stage": stage_value,
                    "source": source,
                },
            )
        run["stage_history"] = history
        run["stage_plan"] = self._updated_stage_plan(
            run.get("stage_plan", []),
            stage_value,
            stage in self.terminal_statuses,
        )
        if stage == EpisodeStatus.completed:
            run["completion_gate"] = readiness
            run["state"] = "completed"
            run["completed_at"] = now
            run["completion_reason"] = "completed"
            control = self._append_workflow_event(
                control,
                "workflow.run.completed",
                {
                    "recorded_at": now,
                    "state": "completed",
                    "stage": stage_value,
                    "completion_reason": "completed",
                },
            )
        elif stage == EpisodeStatus.cancelled:
            run["state"] = "cancelled"
            run["completed_at"] = now
            run["completion_reason"] = "cancelled"
            control = self._append_workflow_event(
                control,
                "workflow.run.completed",
                {
                    "recorded_at": now,
                    "state": "cancelled",
                    "stage": stage_value,
                    "completion_reason": "cancelled",
                },
            )
        control["run"] = run
        episode.workflow_control = control

    def record_failure(
        self,
        episode: Episode,
        failed_stage: EpisodeStatus,
        source: str,
        reason: str,
    ) -> Episode:
        control = self._control(episode)
        now = datetime.now(UTC).isoformat()
        failed_stage_value = failed_stage.value
        control.update(
            {
                "failed_stage": failed_stage_value,
                "failed_at": now,
                "failure_reason": reason,
                "failure_source": source,
            }
        )
        run = control.get("run")
        if isinstance(run, dict):
            run["state"] = "failed"
            run["current_stage"] = EpisodeStatus.failed.value
            run["updated_at"] = now
            run["completed_at"] = now
            run["completion_reason"] = "stage_failed"
            run["failed_stage"] = failed_stage_value
            run["failure_reason"] = reason
            history = list(run.get("stage_history", []))
            if not history or history[-1].get("stage") != EpisodeStatus.failed.value:
                history.append(
                    {
                        "stage": EpisodeStatus.failed.value,
                        "entered_at": now,
                        "source": source,
                    }
                )
            run["stage_history"] = history
            run["stage_plan"] = self._updated_stage_plan(
                run.get("stage_plan", []),
                EpisodeStatus.failed.value,
                terminal=True,
            )
            control["run"] = run
            control = self._append_workflow_event(
                control,
                "workflow.run.completed",
                {
                    "recorded_at": now,
                    "state": "failed",
                    "stage": EpisodeStatus.failed.value,
                    "completion_reason": "stage_failed",
                    "failed_stage": failed_stage_value,
                    "source": source,
                },
            )
        episode.status = EpisodeStatus.failed
        episode.workflow_control = control
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.failed",
                actor="system",
                details={
                    "failed_stage": failed_stage_value,
                    "source": source,
                    "reason": reason,
                },
            )
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor="system",
                details={
                    "stage": EpisodeStatus.failed.value,
                    "previous_stage": failed_stage_value,
                },
            )
        )
        return self._touch(episode)

    def _latest_completed_final_render_asset(self, episode: Episode):
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.render
                and asset.status == "completed"
                and asset.generation_metadata.get("render_type") == "final"
            ),
            None,
        )

    def _latest_completed_preview_render_asset(
        self,
        episode: Episode,
        timeline_asset_id: str | None,
    ):
        if timeline_asset_id is None:
            return None
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.render
                and asset.status == "completed"
                and asset.source_entity_type == "timeline_asset"
                and asset.source_entity_id == timeline_asset_id
                and asset.generation_metadata.get("render_type") == "preview"
                and asset.generation_metadata.get("timeline_asset_id") == timeline_asset_id
            ),
            None,
        )

    def _latest_preview_render_quality_result(self, episode: Episode, preview_render):
        if preview_render is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "render_preview_integrity"
                and result.target_type == "render_asset"
                and result.target_id == str(preview_render.id)
            ),
            None,
        )

    def _latest_final_render_quality_result(self, episode: Episode, final_render):
        if final_render is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "render_final_integrity"
                and result.target_type == "render_asset"
                and result.target_id == str(final_render.id)
            ),
            None,
        )

    def _render_source_asset_freshness(self, render_qc) -> dict:
        if render_qc is None:
            return {
                "fresh": None,
                "stale_source_asset_count": 0,
                "missing_source_asset_count": 0,
            }
        details = render_qc.details if isinstance(render_qc.details, dict) else {}
        has_stale_count = "stale_source_asset_count" in details
        has_missing_count = "missing_source_asset_count" in details
        stale_count = int(details.get("stale_source_asset_count") or 0)
        missing_count = int(details.get("missing_source_asset_count") or 0)
        issues = details.get("issues")
        if isinstance(issues, list):
            if not has_stale_count:
                stale_count = sum(
                    1
                    for issue in issues
                    if isinstance(issue, dict)
                    and issue.get("issue") == "render_source_asset_stale"
                )
            if not has_missing_count:
                missing_count = sum(
                    1
                    for issue in issues
                    if isinstance(issue, dict)
                    and issue.get("issue") == "render_source_asset_missing"
                )
        return {
            "fresh": stale_count == 0 and missing_count == 0,
            "stale_source_asset_count": stale_count,
            "missing_source_asset_count": missing_count,
        }

    def _latest_completed_thumbnail_asset(self, episode: Episode, final_render):
        if final_render is None:
            return None
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.thumbnail
                and asset.status == "completed"
                and asset.source_entity_type == "render_asset"
                and asset.source_entity_id == str(final_render.id)
            ),
            None,
        )

    def _latest_thumbnail_quality_result(self, episode: Episode, thumbnail):
        if thumbnail is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "thumbnail_integrity"
                and result.target_type == "thumbnail_asset"
                and result.target_id == str(thumbnail.id)
            ),
            None,
        )

    def _canonical_or_latest_broadcast_transcript(self, episode: Episode):
        if episode.canonical_transcript_version_id is not None:
            transcript = next(
                (
                    item
                    for item in episode.transcripts
                    if item.id == episode.canonical_transcript_version_id
                ),
                None,
            )
            if transcript is not None:
                return transcript
        return next(
            (
                transcript
                for transcript in reversed(episode.transcripts)
                if transcript.type == TranscriptType.broadcast
            ),
            None,
        )

    def _approved_canonical_transcript(self, episode: Episode):
        transcript = self._canonical_or_latest_broadcast_transcript(episode)
        if transcript is None or transcript.status != "approved":
            return None
        return transcript

    def _latest_approval(self, episode: Episode, stage: str):
        return next(
            (
                approval
                for approval in sorted(
                    episode.approvals,
                    key=lambda item: item.created_at,
                    reverse=True,
                )
                if approval.stage == stage
            ),
            None,
        )

    def _transcript_media_handoff(self, episode: Episode, transcript) -> dict:
        if transcript is None:
            return {
                "playable_turn_count": 0,
                "completed_audio_turn_count": 0,
                "completed_primary_visual_turn_count": 0,
                "missing_audio_turn_ids": [],
                "missing_primary_visual_turn_ids": [],
                "stale_model_turn_ids": [],
                "stale_voice_asset_turn_ids": [],
                "stale_visual_asset_turn_ids": [],
                "expected_reaction_loop_segment_count": 0,
                "linked_reaction_loop_segment_count": 0,
                "missing_reaction_loop_turn_ids": [],
                "expected_studio_scene_segment_count": 0,
                "linked_studio_scene_segment_count": 0,
                "missing_studio_scene_turn_ids": [],
                "subtitle_asset_id": None,
                "timeline_asset_id": None,
                "missing_timeline_segment_turn_ids": [],
            }
        playable_turn_ids = [
            str(turn.id) for turn in transcript.turns if turn.status != "excluded"
        ]
        playable_turn_id_set = set(playable_turn_ids)
        audio_turn_ids = self._completed_audio_turn_ids(
            episode,
            transcript,
            playable_turn_id_set,
        )
        primary_visual_turn_ids = self._completed_primary_visual_turn_ids(
            episode,
            transcript,
            playable_turn_id_set,
        )
        stale_voice_asset_turn_ids = self._voice_profile_mismatch_turn_ids(
            episode,
            transcript,
            playable_turn_id_set,
        )
        stale_visual_asset_turn_ids = self._visual_profile_mismatch_turn_ids(
            episode,
            transcript,
            playable_turn_id_set,
        )
        stale_model_turn_ids = self._model_assignment_mismatch_turn_ids(
            episode,
            transcript,
            playable_turn_id_set,
        )
        subtitle_asset = self._latest_completed_subtitle_asset(episode, transcript)
        timeline_asset = self._latest_completed_timeline_asset(episode, transcript)
        timeline = self._timeline_payload(timeline_asset)
        linked_turn_ids = self._timeline_linked_turn_ids(timeline)
        reusable_visual_handoff = self._shot_planned_reusable_visual_handoff(
            episode,
            transcript,
            timeline,
            playable_turn_id_set,
        )
        return {
            "playable_turn_count": len(playable_turn_ids),
            "completed_audio_turn_count": len(audio_turn_ids),
            "completed_primary_visual_turn_count": len(primary_visual_turn_ids),
            "missing_audio_turn_ids": sorted(playable_turn_id_set - audio_turn_ids),
            "missing_primary_visual_turn_ids": sorted(
                playable_turn_id_set - primary_visual_turn_ids
            ),
            "stale_model_turn_ids": stale_model_turn_ids,
            "stale_voice_asset_turn_ids": stale_voice_asset_turn_ids,
            "stale_visual_asset_turn_ids": stale_visual_asset_turn_ids,
            **reusable_visual_handoff,
            "subtitle_asset_id": str(subtitle_asset.id) if subtitle_asset else None,
            "timeline_asset_id": str(timeline_asset.id) if timeline_asset else None,
            "missing_timeline_segment_turn_ids": sorted(
                playable_turn_id_set - linked_turn_ids
            ),
        }

    def _character_config_handoff(self, episode: Episode, transcript) -> dict:
        if transcript is None:
            return {
                "schema_version": "character_configuration_handoff.v1",
                "ready": False,
                "policy": "each_playable_speaker_requires_model_voice_and_visual_profile",
                "active_speaker_count": 0,
                "configured_model_speaker_count": 0,
                "configured_voice_speaker_count": 0,
                "configured_visual_speaker_count": 0,
                "unknown_speaker_participant_ids": [],
                "missing_model_participant_ids": [],
                "missing_voice_participant_ids": [],
                "missing_visual_participant_ids": [],
                "participants": [],
            }

        participant_by_id = {participant.id: participant for participant in episode.participants}
        active_speaker_ids = sorted(
            {
                turn.speaker_participant_id
                for turn in transcript.turns
                if turn.status != "excluded" and turn.speaker_participant_id
            }
        )
        unknown_speaker_ids = [
            participant_id
            for participant_id in active_speaker_ids
            if participant_id not in participant_by_id
        ]
        participant_entries = []
        missing_model_ids = []
        missing_voice_ids = []
        missing_visual_ids = []

        for participant_id in active_speaker_ids:
            participant = participant_by_id.get(participant_id)
            if participant is None:
                participant_entries.append(
                    {
                        "participant_id": participant_id,
                        "display_name": None,
                        "participant_type": None,
                        "model_endpoint_id": None,
                        "model_id": None,
                        "voice_profile_id": None,
                        "visual_profile_id": None,
                        "model_ready": False,
                        "voice_ready": False,
                        "visual_ready": False,
                    }
                )
                continue

            model_ready = bool(participant.model_endpoint_id and participant.model_id)
            voice_ready = bool(participant.voice_profile_id)
            visual_ready = bool(participant.visual_profile_id)
            if not model_ready:
                missing_model_ids.append(participant_id)
            if not voice_ready:
                missing_voice_ids.append(participant_id)
            if not visual_ready:
                missing_visual_ids.append(participant_id)
            participant_entries.append(
                {
                    "participant_id": participant_id,
                    "display_name": participant.display_name,
                    "participant_type": participant.participant_type.value
                    if hasattr(participant.participant_type, "value")
                    else str(participant.participant_type),
                    "model_endpoint_id": participant.model_endpoint_id,
                    "model_id": participant.model_id,
                    "voice_profile_id": participant.voice_profile_id,
                    "visual_profile_id": participant.visual_profile_id,
                    "model_ready": model_ready,
                    "voice_ready": voice_ready,
                    "visual_ready": visual_ready,
                }
            )

        ready = (
            bool(active_speaker_ids)
            and not unknown_speaker_ids
            and not missing_model_ids
            and not missing_voice_ids
            and not missing_visual_ids
        )
        return {
            "schema_version": "character_configuration_handoff.v1",
            "ready": ready,
            "policy": "each_playable_speaker_requires_model_voice_and_visual_profile",
            "active_speaker_count": len(active_speaker_ids),
            "configured_model_speaker_count": len(active_speaker_ids) - len(missing_model_ids),
            "configured_voice_speaker_count": len(active_speaker_ids) - len(missing_voice_ids),
            "configured_visual_speaker_count": len(active_speaker_ids) - len(missing_visual_ids),
            "unknown_speaker_participant_ids": unknown_speaker_ids,
            "missing_model_participant_ids": missing_model_ids,
            "missing_voice_participant_ids": missing_voice_ids,
            "missing_visual_participant_ids": missing_visual_ids,
            "participants": participant_entries,
        }

    def _completed_audio_turn_ids(
        self,
        episode: Episode,
        transcript,
        playable_turn_ids: set[str],
    ):
        return {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.status == "completed"
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in playable_turn_ids
            and asset.language == transcript.language
            and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        }

    def _completed_primary_visual_turn_ids(
        self,
        episode: Episode,
        transcript,
        playable_turn_ids: set[str],
    ):
        return {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.video
            and asset.status == "completed"
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in playable_turn_ids
            and asset.language == transcript.language
            and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            and asset.generation_metadata.get("visual_role") == "video_primary"
        }

    def _voice_profile_mismatch_turn_ids(
        self,
        episode: Episode,
        transcript,
        playable_turn_ids: set[str],
    ) -> list[str]:
        participant_by_id = {participant.id: participant for participant in episode.participants}
        speaker_by_turn_id = {
            str(turn.id): turn.speaker_participant_id
            for turn in transcript.turns
            if str(turn.id) in playable_turn_ids
        }
        latest_audio_by_turn_id = self._latest_completed_turn_assets(
            episode,
            transcript,
            playable_turn_ids,
            AssetType.audio,
        )
        stale_turn_ids = []
        for turn_id, asset in latest_audio_by_turn_id.items():
            participant = participant_by_id.get(speaker_by_turn_id.get(turn_id) or "")
            expected_profile_id = participant.voice_profile_id if participant else None
            actual_profile_id = asset.generation_metadata.get("voice_profile_id")
            if (
                isinstance(expected_profile_id, str)
                and expected_profile_id
                and isinstance(actual_profile_id, str)
                and actual_profile_id
                and actual_profile_id != expected_profile_id
            ):
                stale_turn_ids.append(turn_id)
        return sorted(stale_turn_ids)

    def _visual_profile_mismatch_turn_ids(
        self,
        episode: Episode,
        transcript,
        playable_turn_ids: set[str],
    ) -> list[str]:
        participant_by_id = {participant.id: participant for participant in episode.participants}
        speaker_by_turn_id = {
            str(turn.id): turn.speaker_participant_id
            for turn in transcript.turns
            if str(turn.id) in playable_turn_ids
        }
        latest_visual_by_turn_id = self._latest_completed_turn_assets(
            episode,
            transcript,
            playable_turn_ids,
            AssetType.video,
            visual_role="video_primary",
        )
        stale_turn_ids = []
        for turn_id, asset in latest_visual_by_turn_id.items():
            participant = participant_by_id.get(speaker_by_turn_id.get(turn_id) or "")
            expected_profile_id = participant.visual_profile_id if participant else None
            actual_profile_id = asset.generation_metadata.get("visual_profile_id")
            if (
                isinstance(expected_profile_id, str)
                and expected_profile_id
                and isinstance(actual_profile_id, str)
                and actual_profile_id
                and actual_profile_id != expected_profile_id
            ):
                stale_turn_ids.append(turn_id)
        return sorted(stale_turn_ids)

    def _model_assignment_mismatch_turn_ids(
        self,
        episode: Episode,
        transcript,
        playable_turn_ids: set[str],
    ) -> list[str]:
        if episode.discussion_session is None:
            return []
        participant_by_id = {participant.id: participant for participant in episode.participants}
        discussion_turn_by_id = {
            str(turn.id): turn for turn in episode.discussion_session.turns
        }
        stale_turn_ids = []
        for transcript_turn in transcript.turns:
            turn_id = str(transcript_turn.id)
            if turn_id not in playable_turn_ids:
                continue
            participant = participant_by_id.get(transcript_turn.speaker_participant_id)
            if participant is None:
                continue
            source_turn = next(
                (
                    discussion_turn_by_id.get(str(source_id))
                    for source_id in transcript_turn.source_discussion_turn_ids
                    if discussion_turn_by_id.get(str(source_id)) is not None
                ),
                None,
            )
            if source_turn is None:
                continue
            metadata = source_turn.generation_metadata
            actual_endpoint_id = metadata.get("model_endpoint_id")
            actual_model_id = metadata.get("model_id")
            if (
                isinstance(actual_endpoint_id, str)
                and actual_endpoint_id
                and actual_endpoint_id != participant.model_endpoint_id
            ):
                stale_turn_ids.append(turn_id)
                continue
            if (
                isinstance(actual_model_id, str)
                and actual_model_id
                and actual_model_id != participant.model_id
            ):
                stale_turn_ids.append(turn_id)
        return sorted(stale_turn_ids)

    def _latest_completed_turn_assets(
        self,
        episode: Episode,
        transcript,
        playable_turn_ids: set[str],
        asset_type: AssetType,
        visual_role: str | None = None,
    ) -> dict[str, Asset]:
        assets_by_turn_id: dict[str, Asset] = {}
        for asset in reversed(episode.assets):
            if asset.source_entity_id in assets_by_turn_id:
                continue
            if asset.asset_type != asset_type:
                continue
            if asset.status != "completed":
                continue
            if asset.language != transcript.language:
                continue
            if asset.source_entity_type != "transcript_turn":
                continue
            if asset.source_entity_id not in playable_turn_ids:
                continue
            if asset.generation_metadata.get("transcript_version_id") != str(transcript.id):
                continue
            if (
                visual_role is not None
                and asset.generation_metadata.get("visual_role") != visual_role
            ):
                continue
            assets_by_turn_id[asset.source_entity_id] = asset
        return assets_by_turn_id

    def _latest_completed_subtitle_asset(self, episode: Episode, transcript):
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.subtitle
                and asset.status == "completed"
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_version"
                and asset.source_entity_id == str(transcript.id)
            ),
            None,
        )

    def _latest_completed_timeline_asset(self, episode: Episode, transcript):
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.timeline
                and asset.status == "completed"
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_version"
                and asset.source_entity_id == str(transcript.id)
            ),
            None,
        )

    def _timeline_payload(self, timeline_asset) -> dict | None:
        if timeline_asset is None:
            return None
        timeline = timeline_asset.generation_metadata.get("timeline_json")
        return timeline if isinstance(timeline, dict) else None

    def _timeline_linked_turn_ids(self, timeline: dict | None) -> set[str]:
        if timeline is None:
            return set()
        return {
            segment["source_turn_id"]
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict) and isinstance(segment.get("source_turn_id"), str)
        }

    def _shot_planned_reusable_visual_handoff(
        self,
        episode: Episode,
        transcript,
        timeline: dict | None,
        playable_turn_ids: set[str],
    ) -> dict:
        if timeline is None:
            return {
                "expected_reaction_loop_segment_count": 0,
                "linked_reaction_loop_segment_count": 0,
                "missing_reaction_loop_turn_ids": [],
                "expected_studio_scene_segment_count": 0,
                "linked_studio_scene_segment_count": 0,
                "missing_studio_scene_turn_ids": [],
            }

        segments_by_turn_id = {
            segment.get("source_turn_id"): segment
            for segment in timeline.get("segments", [])
            if isinstance(segment, dict) and isinstance(segment.get("source_turn_id"), str)
        }
        assets_by_id = {
            str(asset.id): asset for asset in episode.assets if asset.status != "replaced"
        }
        primary_assets_by_turn_id = {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type == AssetType.video
            and asset.status == "completed"
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in playable_turn_ids
            and asset.language == transcript.language
            and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            and asset.generation_metadata.get("visual_role") == "video_primary"
        }
        expected_reaction_turn_ids = set()
        linked_reaction_turn_ids = set()
        expected_studio_turn_ids = set()
        linked_studio_turn_ids = set()

        for turn_id, primary_asset in primary_assets_by_turn_id.items():
            shot_plan = primary_asset.generation_metadata.get("shot_plan")
            if not isinstance(shot_plan, dict):
                continue
            segment = segments_by_turn_id.get(turn_id)
            expected_reaction_id = self._non_empty_string(
                shot_plan.get("reusable_reaction_asset_id")
            )
            if expected_reaction_id is not None:
                expected_reaction_turn_ids.add(turn_id)
                expected_reaction = assets_by_id.get(expected_reaction_id)
                if (
                    segment is not None
                    and segment.get("reaction_visual_asset_id") == expected_reaction_id
                    and self._asset_completed_render_ready(
                        expected_reaction,
                        AssetType.reaction_loop,
                    )
                ):
                    linked_reaction_turn_ids.add(turn_id)
            expected_studio_id = self._non_empty_string(shot_plan.get("studio_scene_asset_id"))
            if expected_studio_id is not None:
                expected_studio_turn_ids.add(turn_id)
                expected_studio = assets_by_id.get(expected_studio_id)
                if (
                    segment is not None
                    and segment.get("studio_scene_asset_id") == expected_studio_id
                    and self._asset_completed_render_ready(
                        expected_studio,
                        AssetType.studio_scene,
                    )
                ):
                    linked_studio_turn_ids.add(turn_id)

        return {
            "expected_reaction_loop_segment_count": len(expected_reaction_turn_ids),
            "linked_reaction_loop_segment_count": len(linked_reaction_turn_ids),
            "missing_reaction_loop_turn_ids": sorted(
                expected_reaction_turn_ids - linked_reaction_turn_ids
            ),
            "expected_studio_scene_segment_count": len(expected_studio_turn_ids),
            "linked_studio_scene_segment_count": len(linked_studio_turn_ids),
            "missing_studio_scene_turn_ids": sorted(
                expected_studio_turn_ids - linked_studio_turn_ids
            ),
        }

    def _non_empty_string(self, value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    def _asset_completed_render_ready(self, asset, asset_type: AssetType) -> bool:
        return (
            asset is not None
            and asset.asset_type == asset_type
            and asset.status == "completed"
            and asset.generation_metadata.get("render_ready") is not False
        )

    def _final_render_linked_to_timeline(
        self,
        final_render,
        timeline_asset_id: str | None,
    ) -> bool | None:
        if final_render is None or timeline_asset_id is None:
            return None
        return (
            final_render.source_entity_type == "timeline_asset"
            and final_render.source_entity_id == timeline_asset_id
            and final_render.generation_metadata.get("timeline_asset_id") == timeline_asset_id
        )

    def _latest_completed_evidence_pack_asset(self, episode: Episode):
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
            ),
            None,
        )

    def _latest_evidence_pack_quality_result(self, episode: Episode, evidence_pack):
        if evidence_pack is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "evidence_pack_integrity"
                and result.target_type == "evidence_pack_asset"
                and result.target_id == str(evidence_pack.id)
            ),
            None,
        )

    def _latest_claim_quality_result(self, episode: Episode, transcript, evidence_pack):
        if transcript is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "claim_citation_integrity"
                and result.target_type == "transcript_version"
                and result.target_id == str(transcript.id)
                and (
                    evidence_pack is None
                    or result.details.get("evidence_pack_asset_id") == str(evidence_pack.id)
                )
            ),
            None,
        )

    def _latest_discussion_quality_result(self, episode: Episode):
        session = episode.discussion_session
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "discussion_minimum_structure"
                and result.target_type == "discussion_session"
                and (session is None or result.target_id == str(session.id))
            ),
            None,
        )

    def _latest_transcript_quality_result(
        self,
        episode: Episode,
        transcript,
        check_type: str,
    ):
        if transcript is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == check_type
                and result.target_type == "transcript_version"
                and result.target_id == str(transcript.id)
            ),
            None,
        )

    def _localized_output_readiness(self, episode: Episode) -> dict:
        required_outputs = [
            output
            for output in episode.definition.languages.outputs
            if output.language != episode.source_language or output.mode != "canonical"
        ]
        outputs = []
        missing_languages = []
        not_approved_languages = []
        qc_missing_languages = []
        qc_failing_languages = []
        for output in required_outputs:
            transcript = self._latest_localized_transcript(episode, output.language)
            qc = self._latest_transcript_quality_result(
                episode,
                transcript,
                "localized_transcript_semantic_fidelity",
            )
            status = transcript.status if transcript is not None else None
            qc_status = qc.status if qc is not None else None
            if transcript is None:
                missing_languages.append(output.language)
            elif status != "approved":
                not_approved_languages.append(output.language)
            if transcript is not None and qc is None:
                qc_missing_languages.append(output.language)
            elif qc is not None and (
                qc.status == "fail" or qc.severity == QualitySeverity.fail
            ):
                qc_failing_languages.append(output.language)
            outputs.append(
                {
                    "language": output.language,
                    "mode": output.mode,
                    "transcript_version_id": str(transcript.id) if transcript else None,
                    "transcript_status": status,
                    "qc_id": str(qc.id) if qc else None,
                    "qc_status": qc_status,
                }
            )
        return {
            "schema_version": "localized_output_readiness.v1",
            "required": bool(required_outputs),
            "required_language_count": len(required_outputs),
            "approved_language_count": sum(
                1 for item in outputs if item["transcript_status"] == "approved"
            ),
            "missing_languages": missing_languages,
            "not_approved_languages": not_approved_languages,
            "qc_missing_languages": qc_missing_languages,
            "qc_failing_languages": qc_failing_languages,
            "outputs": outputs,
        }

    def _latest_localized_transcript(self, episode: Episode, language: str):
        return next(
            (
                transcript
                for transcript in reversed(episode.transcripts)
                if transcript.type == TranscriptType.localized and transcript.language == language
            ),
            None,
        )

    def _latest_subtitle_quality_result(self, episode: Episode, subtitle_asset_id: str | None):
        if subtitle_asset_id is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "subtitle_generation_completeness"
                and result.target_type == "asset"
                and result.target_id == subtitle_asset_id
            ),
            None,
        )

    def _latest_timeline_quality_result(self, episode: Episode, timeline_asset_id: str | None):
        if timeline_asset_id is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "timeline_integrity"
                and result.target_type == "timeline_asset"
                and result.target_id == timeline_asset_id
            ),
            None,
        )

    def _latest_completed_export_package_asset(self, episode: Episode, render_asset):
        if render_asset is None:
            return None
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.export_package
                and asset.status == "completed"
                and asset.source_entity_type == "render_asset"
                and asset.source_entity_id == str(render_asset.id)
            ),
            None,
        )

    def _latest_completed_production_manifest_asset(self, episode: Episode, package_asset):
        if package_asset is None:
            return None
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.production_manifest
                and asset.status == "completed"
                and asset.source_entity_type == "export_package"
                and asset.source_entity_id == str(package_asset.id)
            ),
            None,
        )

    def _latest_package_quality_result(self, episode: Episode, package_asset):
        if package_asset is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "youtube_package_integrity"
                and result.target_id == str(package_asset.id)
            ),
            None,
        )

    def _export_package_includes_thumbnail(self, package_asset, thumbnail) -> bool:
        if package_asset is None or thumbnail is None:
            return False
        metadata = package_asset.generation_metadata
        package_thumbnail_id = metadata.get("thumbnail_asset_id")
        if package_thumbnail_id is not None and str(package_thumbnail_id) != str(thumbnail.id):
            return False
        manifest = metadata.get("youtube_package_manifest")
        if isinstance(manifest, dict):
            manifest_thumbnail_id = manifest.get("thumbnail_asset_id")
            if (
                manifest_thumbnail_id is not None
                and str(manifest_thumbnail_id) != str(thumbnail.id)
            ):
                return False
        included_files = metadata.get("included_files")
        if isinstance(included_files, list):
            return "thumbnail/thumbnail.jpg" in included_files
        return package_thumbnail_id is not None or (
            isinstance(manifest, dict) and manifest.get("thumbnail_asset_id") is not None
        )

    def _export_package_includes_subtitles(self, package_asset) -> bool:
        if package_asset is None:
            return False
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

    def _latest_publish_job(self, episode: Episode, package_asset):
        if package_asset is None:
            return None
        return next(
            (
                job
                for job in reversed(episode.publish_jobs)
                if job.package_asset_id == package_asset.id and job.status != "replaced"
            ),
            None,
        )

    def _latest_publish_quality_result(self, episode: Episode, publish_job):
        if publish_job is None:
            return None
        return next(
            (
                result
                for result in reversed(episode.quality_results)
                if result.check_type == "publish_delivery_integrity"
                and result.target_type == "publish_job"
                and result.target_id == str(publish_job.id)
            ),
            None,
        )

    def _production_manifest_asset_valid(
        self,
        manifest_asset,
        package_asset,
        thumbnail=None,
        subtitle_asset_id: str | None = None,
    ) -> dict:
        manifest = manifest_asset.generation_metadata.get("production_manifest")
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
        if str(embedded_package_id) != str(package_asset.id):
            return {
                "valid": False,
                "reason": "embedded delivery package asset_id does not match package asset",
            }
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
        if thumbnail is not None:
            embedded_package_manifest = delivery_package.get("manifest")
            if not isinstance(embedded_package_manifest, dict):
                return {
                    "valid": False,
                    "reason": "embedded delivery package manifest is missing",
                }
            if str(embedded_package_manifest.get("thumbnail_asset_id") or "") != str(
                thumbnail.id
            ):
                return {
                    "valid": False,
                    "reason": "embedded delivery package thumbnail does not match thumbnail asset",
                }
            embedded_files = delivery_package.get("included_files")
            if isinstance(embedded_files, list) and "thumbnail/thumbnail.jpg" not in embedded_files:
                return {
                    "valid": False,
                    "reason": "embedded delivery package thumbnail file is missing",
                }
        if subtitle_asset_id is not None:
            embedded_files = delivery_package.get("included_files")
            if isinstance(embedded_files, list) and not any(
                isinstance(name, str) and name.startswith("subtitles/")
                for name in embedded_files
            ):
                return {
                    "valid": False,
                    "reason": "embedded delivery package subtitle file is missing",
                }
            embedded_package_manifest = delivery_package.get("manifest")
            if not isinstance(embedded_package_manifest, dict):
                return {
                    "valid": False,
                    "reason": "embedded delivery package manifest is missing",
                }
            embedded_subtitles = embedded_package_manifest.get("subtitles")
            if not isinstance(embedded_subtitles, list) or not embedded_subtitles:
                return {
                    "valid": False,
                    "reason": "embedded delivery package subtitle manifest is missing",
                }
        return {"valid": True, "reason": None}

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

    def _production_manifest_talkshow_visuals_valid(self, manifest: dict) -> dict:
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

    def _production_manifest_publish_evidence(
        self,
        manifest_asset,
        publish_job,
        publish_qc,
    ) -> dict:
        if publish_job is None:
            return {"valid": True, "reason": None}
        if manifest_asset is None:
            return {"valid": False, "reason": "production manifest is missing"}
        manifest = manifest_asset.generation_metadata.get("production_manifest")
        if not isinstance(manifest, dict):
            return {"valid": False, "reason": "embedded production_manifest is missing"}
        publish_jobs = manifest.get("publish_jobs")
        if not isinstance(publish_jobs, list):
            return {"valid": False, "reason": "embedded publish_jobs is missing"}
        embedded_job = next(
            (job for job in publish_jobs if str(job.get("id")) == str(publish_job.id)),
            None,
        )
        if embedded_job is None:
            return {
                "valid": False,
                "reason": "embedded publish_jobs does not include latest publish job",
            }
        if embedded_job.get("status") != publish_job.status:
            return {
                "valid": False,
                "reason": "embedded publish job status does not match latest publish job",
            }
        if str(embedded_job.get("package_asset_id")) != str(publish_job.package_asset_id):
            return {
                "valid": False,
                "reason": "embedded publish job package does not match latest publish job",
            }
        if publish_qc is None:
            return {"valid": True, "reason": None}
        quality_results = manifest.get("quality_results")
        if not isinstance(quality_results, list):
            return {"valid": False, "reason": "embedded quality_results is missing"}
        embedded_qc = next(
            (result for result in quality_results if str(result.get("id")) == str(publish_qc.id)),
            None,
        )
        if embedded_qc is None:
            return {
                "valid": False,
                "reason": "embedded quality_results does not include latest publish QC",
            }
        if embedded_qc.get("check_type") != "publish_delivery_integrity":
            return {
                "valid": False,
                "reason": "embedded publish QC check_type is invalid",
            }
        if embedded_qc.get("target_type") != "publish_job":
            return {
                "valid": False,
                "reason": "embedded publish QC target_type is invalid",
            }
        if str(embedded_qc.get("target_id")) != str(publish_job.id):
            return {
                "valid": False,
                "reason": "embedded publish QC target does not match latest publish job",
            }
        if embedded_qc.get("status") != publish_qc.status:
            return {
                "valid": False,
                "reason": "embedded publish QC status does not match latest publish QC",
            }
        return {"valid": True, "reason": None}

    def _unresolved_failed_assets(self, episode: Episode) -> list[dict]:
        failed_statuses = {"failed", "fail", "error", "corrupt", "missing"}
        return [
            {
                "asset_id": str(asset.id),
                "asset_type": asset.asset_type.value,
                "status": asset.status,
                "blocks_completion": self._failed_asset_blocks_completion(episode, asset),
            }
            for asset in episode.assets
            if asset.status in failed_statuses
        ]

    def _resolved_failed_assets(self, episode: Episode) -> list[dict]:
        resolved: list[dict] = []
        replacement_by_original_id = {
            asset.generation_metadata.get("replacement_of_asset_id"): asset
            for asset in episode.assets
            if asset.generation_metadata.get("manual_replacement") is True
            and isinstance(asset.generation_metadata.get("replacement_of_asset_id"), str)
        }
        for asset in episode.assets:
            if asset.status != "replaced":
                continue
            replacement = replacement_by_original_id.get(str(asset.id))
            if replacement is None:
                replacement_id = asset.generation_metadata.get("replaced_by_asset_id")
                replacement = next(
                    (
                        item
                        for item in episode.assets
                        if str(item.id) == str(replacement_id)
                    ),
                    None,
                )
            resolved.append(
                {
                    "asset_id": str(asset.id),
                    "asset_type": asset.asset_type.value,
                    "status": asset.status,
                    "replacement_asset_id": str(replacement.id) if replacement else None,
                    "replacement_status": replacement.status if replacement else None,
                    "replacement_checksum": replacement.checksum if replacement else None,
                    "replacement_storage_uri": replacement.storage_uri if replacement else None,
                    "replacement_ready": replacement is not None
                    and replacement.status == "completed"
                    and bool(replacement.storage_uri)
                    and bool(replacement.checksum),
                    "replacement_reason": asset.generation_metadata.get("replacement_reason"),
                    "replaced_at": asset.generation_metadata.get("replaced_at"),
                }
            )
        return resolved

    def _latest_failing_quality_results(self, episode: Episode) -> list[dict]:
        latest_by_gate: dict[tuple[str, str, str], object] = {}
        for result in episode.quality_results:
            latest_by_gate[(result.target_type, result.target_id, result.check_type)] = result
        return [
            {
                "quality_result_id": str(result.id),
                "target_type": result.target_type,
                "target_id": result.target_id,
                "check_type": result.check_type,
                "status": result.status,
                "severity": result.severity.value,
                "blocks_completion": self._quality_result_blocks_completion(episode, result),
            }
            for result in latest_by_gate.values()
            if result.status == "fail" or result.severity == QualitySeverity.fail
            if self._quality_result_targets_current_completion_artifact(episode, result)
        ]

    def _quality_result_targets_current_completion_artifact(self, episode: Episode, result) -> bool:
        if result.check_type in {
            "audio_asset_plan_completeness",
            "audio_generation_completeness",
        }:
            return True
        if result.check_type == "discussion_duration_control" and (
            self._discussion_duration_failure_superseded(episode, result)
        ):
            return False
        transcript = self._canonical_or_latest_broadcast_transcript(episode)
        evidence_pack = self._latest_completed_evidence_pack_asset(episode)
        subtitle = (
            self._latest_completed_subtitle_asset(episode, transcript)
            if transcript is not None
            else None
        )
        # A failed subtitle gate still describes the current attempt when no
        # completed subtitle has superseded it.  Once a completed subtitle
        # exists, the target id must match that current artifact; this is what
        # lets regenerated subtitles retire historical failures without making
        # a missing-subtitle failure disappear.
        if result.check_type == "subtitle_generation_completeness":
            if subtitle is None:
                return True
            if result.target_id == str(subtitle.id):
                return True
            # A gate recorded after the current subtitle is still actionable,
            # even if an older caller used a synthetic target id.  Only a
            # newer completed subtitle can supersede an earlier failed gate.
            if result.created_at >= subtitle.created_at:
                return True
        timeline = (
            self._latest_completed_timeline_asset(episode, transcript)
            if transcript is not None
            else None
        )
        preview_render = self._latest_completed_preview_render_asset(
            episode,
            str(timeline.id) if timeline is not None else None,
        )
        final_render = self._latest_completed_final_render_asset(episode)
        thumbnail = self._latest_completed_thumbnail_asset(episode, final_render)
        package = self._latest_completed_export_package_asset(episode, final_render)
        manifest = self._latest_completed_production_manifest_asset(episode, package)
        publish_job = self._latest_publish_job(episode, package)
        current_target_ids = {
            "discussion_session": {
                str(episode.discussion_session.id) if episode.discussion_session else None
            },
            "transcript_version": {str(transcript.id) if transcript else None},
            "asset": {str(subtitle.id) if subtitle else None},
            "timeline_asset": {str(timeline.id) if timeline else None},
            "render_asset": {
                str(preview_render.id) if preview_render else None,
                str(final_render.id) if final_render else None,
            },
            "thumbnail_asset": {str(thumbnail.id) if thumbnail else None},
            "export_package_asset": {str(package.id) if package else None},
            "production_manifest_asset": {str(manifest.id) if manifest else None},
            "evidence_pack_asset": {str(evidence_pack.id) if evidence_pack else None},
            "publish_job": {str(publish_job.id) if publish_job else None},
        }
        accepted_ids = current_target_ids.get(result.target_type)
        if accepted_ids is None:
            return True
        return result.target_id in {target_id for target_id in accepted_ids if target_id}

    def _discussion_duration_failure_superseded(self, episode: Episode, result) -> bool:
        failures = result.details.get("failures")
        if not isinstance(failures, list) or not failures:
            return False
        if any(
            not isinstance(failure, dict)
            or failure.get("issue") != "turn_exceeds_maximum_monologue_duration"
            or not failure.get("turn_id")
            for failure in failures
        ):
            return False
        transcript = self._canonical_or_latest_broadcast_transcript(episode)
        timeline = (
            self._latest_completed_timeline_asset(episode, transcript)
            if transcript is not None
            else None
        )
        timeline_json = (
            timeline.generation_metadata.get("timeline_json")
            if timeline is not None
            and isinstance(timeline.generation_metadata, dict)
            else None
        )
        segments = timeline_json.get("segments") if isinstance(timeline_json, dict) else None
        if transcript is None or not isinstance(segments, list):
            return False
        duration_by_turn: dict[str, int] = {}
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            source_turn_id = str(segment.get("source_turn_id") or "").strip()
            duration_ms = segment.get("duration_ms")
            if source_turn_id and isinstance(duration_ms, (int, float)):
                duration_by_turn[source_turn_id] = duration_by_turn.get(source_turn_id, 0) + int(
                    duration_ms
                )
        maximum_seconds = self._numeric_detail(result.details, "maximum_monologue_seconds")
        if maximum_seconds <= 0:
            maximum_seconds = float(episode.definition.format.maximum_monologue_seconds)
        for failure in failures:
            failed_turn_id = str(failure["turn_id"])
            current_turn_ids = [
                str(turn.id)
                for turn in transcript.turns
                if str(turn.id) == failed_turn_id
                or failed_turn_id in {str(item) for item in turn.source_discussion_turn_ids}
            ]
            actual_duration_ms = sum(
                duration_by_turn.get(turn_id, 0) for turn_id in current_turn_ids
            )
            if actual_duration_ms <= 0 or actual_duration_ms > maximum_seconds * 1000:
                return False
        return True

    def _failed_asset_blocks_completion(self, episode: Episode, asset) -> bool:
        generation_metadata = (
            asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
        )
        transcript_version_id = str(generation_metadata.get("transcript_version_id") or "").strip()
        canonical_transcript_version_id = str(
            episode.canonical_transcript_version_id or ""
        ).strip()
        if (
            transcript_version_id
            and canonical_transcript_version_id
            and transcript_version_id != canonical_transcript_version_id
        ):
            return False
        required_for_production = generation_metadata.get("required_for_production")
        if isinstance(required_for_production, bool):
            return required_for_production
        if asset.asset_type == AssetType.audio:
            return episode.definition.quality.block_on_missing_audio
        if asset.asset_type == AssetType.subtitle:
            return episode.definition.quality.block_on_missing_subtitles
        if asset.asset_type == AssetType.render:
            transcript = self._canonical_or_latest_broadcast_transcript(episode)
            timeline = (
                self._latest_completed_timeline_asset(episode, transcript)
                if transcript is not None
                else None
            )
            if timeline is not None and str(asset.source_entity_id) != str(timeline.id):
                return False
        if asset.asset_type in {
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
            AssetType.citation_card,
        }:
            return False
        return True

    def _quality_result_blocks_completion(self, episode: Episode, result) -> bool:
        check_type = result.check_type
        if check_type == "publish_delivery_integrity" and result.details.get("dry_run") is True:
            return False
        if check_type in {
            "audio_asset_plan_completeness",
            "audio_generation_completeness",
            "audio_media_integrity",
        }:
            return episode.definition.quality.block_on_missing_audio
        if check_type == "subtitle_generation_completeness":
            max_sync_error_ms = self._numeric_detail(result.details, "max_sync_error_ms")
            if max_sync_error_ms > episode.definition.quality.block_on_sync_error_ms:
                return True
            return episode.definition.quality.block_on_missing_subtitles
        if check_type == "claim_citation_integrity":
            return (
                episode.definition.quality.block_on_unsupported_high_impact_claims
                and not self._claim_qc_is_editorially_accepted(episode, result)
            )
        return True

    def _claim_qc_is_editorially_accepted(self, episode: Episode, result) -> bool:
        if result.target_type != "transcript_version":
            return False
        transcript = next(
            (
                item
                for item in episode.transcripts
                if str(item.id) == str(result.target_id)
            ),
            None,
        )
        if transcript is None or transcript.status != "approved":
            return False
        return any(
            approval.stage == "transcript_review"
            and approval.decision == "approved"
            and (
                approval.target_id == str(transcript.id)
                or approval.target_id is None
            )
            for approval in episode.approvals
        )

    def _quality_blocking_policy(self, episode: Episode) -> dict:
        quality = episode.definition.quality
        return {
            "schema_version": "quality_completion_policy.v1",
            "block_on_unsupported_high_impact_claims": (
                quality.block_on_unsupported_high_impact_claims
            ),
            "block_on_missing_audio": quality.block_on_missing_audio,
            "block_on_sync_error_ms": quality.block_on_sync_error_ms,
            "block_on_missing_subtitles": quality.block_on_missing_subtitles,
        }

    def _numeric_detail(self, details: dict, key: str) -> float:
        value = details.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0

    def replay_workflow(self, episode: Episode) -> dict:
        control = self._control(episode)
        run = control.get("run")
        event_log = [
            event for event in control.get("workflow_event_log", []) if isinstance(event, dict)
        ]
        replayed = self._replay_events(event_log)
        current = self._current_run_projection(run)
        issues = self._replay_issues(event_log, replayed, current)
        status = "pass" if not issues and event_log and isinstance(run, dict) else "warning"
        if issues:
            status = "fail"
        return {
            "schema_version": "workflow_replay_report.v1",
            "policy": self.external_temporal_policy,
            "episode_id": str(episode.id),
            "status": status,
            "event_count": len(event_log),
            "event_log_checksum": self._event_log_checksum(event_log),
            "replayed": replayed,
            "current": current,
            "issues": issues,
        }

    def record_worker_orchestration(
        self,
        episode: Episode,
        summary: dict,
        worker_id: str = "workflow-worker",
    ) -> Episode:
        control = self._control(episode)
        run = control.get("run")
        if not isinstance(run, dict):
            return episode

        now = datetime.now(UTC)
        summary_id = self._worker_orchestration_summary_id(summary)
        summary_checksum = self._stable_checksum(summary)
        stage_attempts = self._worker_stage_attempts(summary)
        matching_errors = self._worker_errors_for_episode(summary, str(episode.id))
        production_handoff = self._worker_production_handoff_for_episode(
            summary,
            str(episode.id),
        )
        completion_handoff = self._worker_completion_handoff_for_episode(
            summary,
            str(episode.id),
        )
        log = list(control.get("worker_orchestration_log", []))
        if self._worker_orchestration_already_recorded(log, summary_id):
            return episode
        operational_checksum = self._stable_checksum(
            {
                "stage_attempts": stage_attempts,
                "matching_errors": matching_errors,
                "production_handoff": production_handoff,
                "completion_handoff": completion_handoff,
            }
        )
        attempt_sequence = int(run.get("worker_orchestration_attempt_count") or len(log)) + 1
        temporal_dispatches = self._temporal_stage_dispatches(
            episode=episode,
            run=run,
            summary_id=summary_id,
            attempt_sequence=attempt_sequence,
            recorded_at=now.isoformat(),
            worker_id=worker_id,
            stage_attempts=stage_attempts,
        )
        entry = {
            "schema_version": "workflow_worker_orchestration_attempt.v1",
            "summary_id": summary_id,
            "attempt_sequence": attempt_sequence,
            "recorded_at": now.isoformat(),
            "worker_id": worker_id,
            "policy": summary.get("policy"),
            "batch_limit": summary.get("batch_limit"),
            "stage_order": summary.get("stage_order", []),
            "progressed_stage_count": int(summary.get("progressed_stage_count") or 0),
            "error_count": int(summary.get("error_count") or 0),
            "summary_checksum": summary_checksum,
            "stage_attempts": stage_attempts,
            "temporal_dispatch_count": len(temporal_dispatches),
        }
        if production_handoff is not None:
            entry["production_handoff"] = production_handoff
        if completion_handoff is not None:
            entry["completion_handoff"] = completion_handoff
        log.append(entry)
        retained_log = log[-self.worker_orchestration_log_retention_limit :]
        control["worker_orchestration_log"] = retained_log
        control["worker_orchestration_log_retention"] = {
            "retained_attempt_count": len(retained_log),
            "first_retained_attempt_sequence": retained_log[0].get("attempt_sequence"),
            "last_retained_attempt_sequence": retained_log[-1].get("attempt_sequence"),
            "dropped_attempt_count": max(attempt_sequence - len(retained_log), 0),
        }
        run["last_worker_orchestration"] = {
            "summary_id": summary_id,
            "attempt_sequence": attempt_sequence,
            "recorded_at": entry["recorded_at"],
            "worker_id": worker_id,
            "policy": entry["policy"],
            "progressed_stage_count": entry["progressed_stage_count"],
            "error_count": entry["error_count"],
            "summary_checksum": summary_checksum,
            "operational_checksum": operational_checksum,
            "failed_stage_count": len(matching_errors),
            "temporal_dispatch_count": len(temporal_dispatches),
        }
        if production_handoff is not None:
            run["last_worker_orchestration"]["production_handoff"] = production_handoff
        if completion_handoff is not None:
            run["last_worker_orchestration"]["completion_handoff"] = completion_handoff
        run["worker_orchestration_attempt_count"] = attempt_sequence
        run["updated_at"] = entry["recorded_at"]
        control["run"] = run
        if temporal_dispatches:
            dispatch_log = list(control.get("temporal_stage_dispatch_log", []))
            for dispatch in temporal_dispatches:
                dispatch["dispatch_sequence"] = int(
                    run.get("temporal_stage_dispatch_count") or len(dispatch_log)
                ) + 1
                dispatch_log.append(dispatch)
                run["temporal_stage_dispatch_count"] = dispatch["dispatch_sequence"]
            control["temporal_stage_dispatch_log"] = dispatch_log[
                -self.temporal_stage_dispatch_log_retention_limit :
            ]
            ready_count = sum(
                1 for dispatch in temporal_dispatches if dispatch.get("status") == "ready"
            )
            blocked_count = len(temporal_dispatches) - ready_count
            run["last_temporal_stage_dispatch"] = {
                "schema_version": "temporal_stage_dispatch_summary.v1",
                "summary_id": summary_id,
                "attempt_sequence": attempt_sequence,
                "recorded_at": entry["recorded_at"],
                "dispatch_count": len(temporal_dispatches),
                "ready_count": ready_count,
                "blocked_count": blocked_count,
                "namespace": self.settings.temporal_namespace,
                "task_queue": self.settings.temporal_task_queue,
            }
            control["run"] = run
        for error in matching_errors:
            control = self._append_stage_retry(control, error, summary_id, now)
        if temporal_dispatches:
            control = self._append_workflow_event(
                control,
                "workflow.temporal.stage_dispatch_recorded",
                {
                    "recorded_at": entry["recorded_at"],
                    "summary_id": summary_id,
                    "attempt_sequence": attempt_sequence,
                    "dispatch_count": len(temporal_dispatches),
                    "ready_count": sum(
                        1 for dispatch in temporal_dispatches if dispatch.get("status") == "ready"
                    ),
                    "blocked_count": sum(
                        1 for dispatch in temporal_dispatches if dispatch.get("status") == "blocked"
                    ),
                    "namespace": self.settings.temporal_namespace,
                    "task_queue": self.settings.temporal_task_queue,
                },
            )
        episode.workflow_control = control
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.worker.orchestration_recorded",
                actor=worker_id,
                details={
                    "summary_id": summary_id,
                    "attempt_sequence": attempt_sequence,
                    "progressed_stage_count": entry["progressed_stage_count"],
                    "error_count": entry["error_count"],
                    "failed_stage_count": len(matching_errors),
                    "summary_checksum": summary_checksum,
                    "temporal_dispatch_count": len(temporal_dispatches),
                },
            )
        )
        return self._touch(episode)

    def _control(self, episode: Episode) -> dict:
        return dict(episode.workflow_control or {})

    def _worker_stage_attempts(self, summary: dict) -> list[dict]:
        stages = summary.get("stages", {})
        if not isinstance(stages, dict):
            return []
        stage_order = summary.get("stage_order")
        if not isinstance(stage_order, list):
            stage_order = list(stages)
        attempts = []
        for stage in stage_order:
            stage_summary = stages.get(stage, {})
            if not isinstance(stage_summary, dict):
                continue
            progress_count = self._worker_stage_progress_count(str(stage), stage_summary)
            error_count = int(stage_summary.get("error_count") or 0)
            status = "failed" if error_count else "progressed" if progress_count else "idle"
            summary_checksum = self._stable_checksum(stage_summary)
            attempts.append(
                {
                    "stage": str(stage),
                    "status": status,
                    "progress_count": progress_count,
                    "error_count": error_count,
                    "episodes_scanned": int(stage_summary.get("episodes_scanned") or 0),
                    "skipped": int(stage_summary.get("skipped") or 0),
                    "workflow_blocked": int(stage_summary.get("workflow_blocked") or 0),
                    "targeted_audio_assets": int(
                        stage_summary.get("targeted_audio_assets") or 0
                    ),
                    "repair_audio_assets": int(
                        stage_summary.get("repair_audio_assets") or 0
                    ),
                    "targeted_visual_assets": int(
                        stage_summary.get("targeted_visual_assets") or 0
                    ),
                    "repair_visual_assets": int(
                        stage_summary.get("repair_visual_assets") or 0
                    ),
                    "summary_checksum": summary_checksum,
                    "stage_manifest": self._worker_stage_manifest(
                        stage=str(stage),
                        status=status,
                        progress_count=progress_count,
                        error_count=error_count,
                        stage_summary=stage_summary,
                        summary_checksum=summary_checksum,
                    ),
                }
            )
        return attempts

    def _worker_stage_manifest(
        self,
        *,
        stage: str,
        status: str,
        progress_count: int,
        error_count: int,
        stage_summary: dict,
        summary_checksum: str,
    ) -> dict:
        progress_metrics = {
            key: value
            for key, value in sorted(stage_summary.items())
            if key
            not in {
                "errors",
                "error_count",
                "episodes_scanned",
                "skipped",
                "workflow_blocked",
            }
            and isinstance(value, (int, float, bool))
        }
        raw_errors = stage_summary.get("errors", [])
        if not isinstance(raw_errors, list):
            raw_errors = []
        errors = [
            {
                key: value
                for key, value in error.items()
                if key
                in {
                    "episode_id",
                    "stage",
                    "error",
                    "error_kind",
                    "retry_id",
                    "status",
                }
            }
            for error in raw_errors
            if isinstance(error, dict)
        ]
        manifest = {
            "schema_version": "workflow_stage_manifest.v1",
            "stage": stage,
            "status": status,
            "progress_count": progress_count,
            "error_count": error_count,
            "episodes_scanned": int(stage_summary.get("episodes_scanned") or 0),
            "skipped": int(stage_summary.get("skipped") or 0),
            "workflow_blocked": int(stage_summary.get("workflow_blocked") or 0),
            "progress_metrics": progress_metrics,
            "errors": errors,
            "summary_checksum": summary_checksum,
        }
        manifest["manifest_checksum"] = self._stable_checksum(manifest)
        return manifest

    def _temporal_stage_dispatches(
        self,
        episode: Episode,
        run: dict,
        summary_id: str,
        attempt_sequence: int,
        recorded_at: str,
        worker_id: str,
        stage_attempts: list[dict],
    ) -> list[dict]:
        if self.settings.temporal_backend_mode.strip().lower() != "external":
            return []
        dispatches = []
        for stage_attempt in stage_attempts:
            stage = str(stage_attempt.get("stage") or "unknown")
            missing = self._temporal_dispatch_missing_settings()
            status = "blocked" if missing else "ready"
            dispatch_payload = {
                "episode_id": str(episode.id),
                "run_id": run.get("run_id"),
                "run_sequence": run.get("run_sequence"),
                "stage": stage,
                "target_stage": self._worker_stage_status_value(stage),
                "activity_name": self._temporal_activity_name(stage),
                "stage_attempt": stage_attempt,
            }
            dispatches.append(
                {
                    "schema_version": "temporal_stage_dispatch.v1",
                    "dispatch_id": str(uuid4()),
                    "summary_id": summary_id,
                    "orchestration_attempt_sequence": attempt_sequence,
                    "requested_at": recorded_at,
                    "requested_by": worker_id,
                    "status": status,
                    "missing": missing,
                    "reason": self._temporal_dispatch_reason(missing),
                    "namespace": self.settings.temporal_namespace,
                    "task_queue": self.settings.temporal_task_queue,
                    "backend_address": self.settings.temporal_backend_address,
                    "native_worker_enabled": self.settings.temporal_backend_worker_enabled,
                    "idempotency_key": self._stable_checksum(dispatch_payload),
                    **dispatch_payload,
                }
            )
        return dispatches

    def _temporal_dispatch_missing_settings(self) -> list[str]:
        missing = []
        if not self.settings.temporal_backend_address:
            missing.append("DIALECTICORE_TEMPORAL_BACKEND_ADDRESS")
        if not self.settings.temporal_task_queue:
            missing.append("DIALECTICORE_TEMPORAL_TASK_QUEUE")
        if not self.settings.temporal_backend_worker_enabled:
            missing.append("DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED")
        return missing

    def _temporal_dispatch_reason(self, missing: list[str]) -> str:
        if not missing:
            return "external Temporal stage dispatch is ready for native worker pickup"
        return "external Temporal stage dispatch is blocked by missing runtime settings"

    def _temporal_activity_name(self, stage: str) -> str:
        return f"dialecticore.production.{stage}"

    def _worker_stage_progress_count(self, stage: str, summary: dict) -> int:
        return worker_stage_progress_count(stage, summary)

    def _worker_errors_for_episode(self, summary: dict, episode_id: str) -> list[dict]:
        stages = summary.get("stages", {})
        if not isinstance(stages, dict):
            return []
        errors = []
        for stage, stage_summary in stages.items():
            if not isinstance(stage_summary, dict):
                continue
            for error in stage_summary.get("errors", []):
                if isinstance(error, dict) and str(error.get("episode_id")) == episode_id:
                    errors.append({"stage": str(stage), **error})
        return errors

    def _worker_production_handoff_for_episode(
        self,
        summary: dict,
        episode_id: str,
    ) -> dict | None:
        handoffs = summary.get("production_handoffs")
        if not isinstance(handoffs, list):
            return None
        for handoff in handoffs:
            if isinstance(handoff, dict) and str(handoff.get("episode_id")) == episode_id:
                return handoff
        return None

    def _worker_completion_handoff_for_episode(
        self,
        summary: dict,
        episode_id: str,
    ) -> dict | None:
        stages = summary.get("stages")
        if not isinstance(stages, dict):
            return None
        completion = stages.get("completion")
        if not isinstance(completion, dict):
            return None
        completed_ids = {
            str(value)
            for value in completion.get("completed_episode_ids", [])
            if value
        }
        if episode_id in completed_ids:
            return {
                "schema_version": "workflow_completion_handoff.v1",
                "episode_id": episode_id,
                "status": "completed",
                "failed_checks": [],
            }
        blockers = completion.get("readiness_blockers")
        if not isinstance(blockers, list):
            return None
        for blocker in blockers:
            if not isinstance(blocker, dict) or str(blocker.get("episode_id")) != episode_id:
                continue
            failed_checks = blocker.get("failed_checks")
            return {
                "schema_version": "workflow_completion_handoff.v1",
                "episode_id": episode_id,
                "status": "blocked",
                "failed_checks": list(failed_checks) if isinstance(failed_checks, list) else [],
            }
        return None

    def _append_stage_retry(
        self,
        control: dict,
        error: dict,
        summary_id: str,
        now: datetime,
    ) -> dict:
        stage = str(error.get("stage") or "unknown")
        retry_queue = list(control.get("stage_retry_queue", []))
        attempt_number = self._next_stage_retry_attempt_number(retry_queue, stage)
        max_attempts = self.settings.workflow_stage_retry_max_attempts
        exhausted = attempt_number >= max_attempts
        backoff_seconds = self.settings.workflow_stage_retry_backoff_seconds * attempt_number
        retry_entry = {
            "schema_version": "workflow_stage_retry.v1",
            "retry_id": str(uuid4()),
            "stage": stage,
            "target_stage": self._worker_stage_status_value(stage),
            "source_summary_id": summary_id,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "status": "exhausted" if exhausted else "scheduled",
            "created_at": now.isoformat(),
            "next_retry_not_before": (
                None if exhausted else (now + timedelta(seconds=backoff_seconds)).isoformat()
            ),
            "backoff_seconds": backoff_seconds,
            "error": error.get("error"),
            "details": {key: value for key, value in error.items() if key not in {"error"}},
        }
        retry_queue.append(retry_entry)
        control["stage_retry_queue"] = retry_queue
        control["failed_stage"] = retry_entry["target_stage"]
        control["last_stage_retry"] = retry_entry
        if exhausted:
            control["retry_exhausted"] = True
            control["retry_exhausted_stage"] = retry_entry["target_stage"]
        run = control.get("run")
        stage_plan = run.get("stage_plan") if isinstance(run, dict) else None
        if isinstance(stage_plan, list):
            for item in stage_plan:
                if isinstance(item, dict) and item.get("stage") == retry_entry["target_stage"]:
                    item["failure_count"] = int(item.get("failure_count") or 0) + 1
                    item["last_error"] = retry_entry["error"]
                    item["last_failed_at"] = retry_entry["created_at"]
                    item["retry_status"] = retry_entry["status"]
        return control

    def _next_stage_retry_attempt_number(self, retry_queue: list, stage: str) -> int:
        max_attempt = 0
        unnumbered_attempts = 0
        for item in retry_queue:
            if not isinstance(item, dict) or item.get("stage") != stage:
                continue
            try:
                attempt = int(item.get("attempt_number") or 0)
            except (TypeError, ValueError):
                attempt = 0
            if attempt > 0:
                max_attempt = max(max_attempt, attempt)
            else:
                unnumbered_attempts += 1
        return max(max_attempt, unnumbered_attempts) + 1

    def _worker_stage_status_value(self, stage: str) -> str:
        return {
            "research": EpisodeStatus.researching.value,
            "discussion": EpisodeStatus.discussing.value,
            "localization": EpisodeStatus.localizing.value,
            "qc": EpisodeStatus.transcript_qc.value,
            "audio": EpisodeStatus.generating_audio.value,
            "voicebox": EpisodeStatus.generating_audio.value,
            "subtitles": EpisodeStatus.ready.value,
            "visuals": EpisodeStatus.generating_visuals.value,
            "comfyui": EpisodeStatus.generating_visuals.value,
            "timeline": EpisodeStatus.building_timeline.value,
            "render": EpisodeStatus.rendering_final.value,
            "publishing": EpisodeStatus.exporting.value,
            "completion": EpisodeStatus.completed.value,
        }.get(stage, EpisodeStatus.failed.value)

    def _next_due_stage_retry(self, control: dict, now: datetime) -> dict | None:
        retry_queue = control.get("stage_retry_queue", [])
        if not isinstance(retry_queue, list):
            return None
        due = []
        for item in retry_queue:
            if not isinstance(item, dict) or item.get("status") != "scheduled":
                continue
            retry_at = self._parse_retry_timestamp(item.get("next_retry_not_before"))
            if retry_at is None or retry_at > now:
                continue
            due.append((retry_at, str(item.get("retry_id") or ""), item))
        if not due:
            return None
        due.sort(key=lambda entry: (entry[0], entry[1]))
        return dict(due[0][2])

    def _parse_retry_timestamp(self, value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _stage_plan(self) -> list[dict]:
        return [
            {
                "id": stage_id,
                "stage": stage,
                "label": label,
                "status": "pending",
                "attempt": 0,
            }
            for stage_id, stage, label in self.stage_plan
        ]

    def _updated_stage_plan(
        self,
        stage_plan: object,
        current_stage: str,
        terminal: bool = False,
    ) -> list[dict]:
        if not isinstance(stage_plan, list):
            stage_plan = self._stage_plan()
        stage_order = [stage for _, stage, _ in self.stage_plan]
        current_index = stage_order.index(current_stage) if current_stage in stage_order else None
        updated = []
        for item in stage_plan:
            if not isinstance(item, dict):
                continue
            stage = item.get("stage")
            item_index = stage_order.index(stage) if stage in stage_order else None
            status = "pending"
            if current_index is not None and item_index is not None:
                if item_index < current_index or terminal:
                    status = "completed"
                elif item_index == current_index:
                    status = "running"
            previous_status = item.get("status")
            updated_item = {**item, "status": status}
            if status == "running" and previous_status != "running":
                updated_item["attempt"] = int(updated_item.get("attempt") or 0) + 1
            updated.append(updated_item)
        return updated

    def _reopen_run_after_operator_action(
        self,
        control: dict,
        stage: str,
        source: str,
        recorded_at: str,
    ) -> dict:
        run = control.get("run")
        if not isinstance(run, dict):
            return control
        run["state"] = "running"
        run["current_stage"] = stage
        run["updated_at"] = recorded_at
        run["completed_at"] = None
        run["completion_reason"] = None
        history = list(run.get("stage_history", []))
        if not history or history[-1].get("stage") != stage:
            history.append({"stage": stage, "entered_at": recorded_at, "source": source})
            control = self._append_workflow_event(
                control,
                "workflow.stage.entered",
                {
                    "recorded_at": recorded_at,
                    "stage": stage,
                    "source": source,
                },
            )
        run["stage_history"] = history
        run["stage_plan"] = self._updated_stage_plan(run.get("stage_plan", []), stage)
        control["run"] = run
        return control

    def _resolve_stage_retries(
        self,
        control: dict,
        target_stage: str,
        resolution: str,
        resolved_at: str,
        actor: str,
        signal_id: str,
    ) -> dict:
        retry_queue = control.get("stage_retry_queue", [])
        if not isinstance(retry_queue, list):
            return control
        resolved_count = 0
        updated_queue = []
        for item in retry_queue:
            if not isinstance(item, dict):
                updated_queue.append(item)
                continue
            if item.get("target_stage") == target_stage and item.get("status") in {
                "scheduled",
                "exhausted",
            }:
                previous_status = item.get("status")
                item = {
                    **item,
                    "status": resolution,
                    "previous_status": previous_status,
                    "resolved_at": resolved_at,
                    "resolved_by": actor,
                    "resolution_signal_id": signal_id,
                    "next_retry_not_before": None,
                }
                resolved_count += 1
            updated_queue.append(item)
        if resolved_count == 0:
            return control
        control["stage_retry_queue"] = updated_queue
        control["last_stage_retry_resolution"] = {
            "schema_version": "workflow_stage_retry_resolution.v1",
            "target_stage": target_stage,
            "resolution": resolution,
            "resolved_at": resolved_at,
            "resolved_by": actor,
            "resolution_signal_id": signal_id,
            "resolved_count": resolved_count,
        }
        control = self._append_workflow_event(
            control,
            "workflow.stage_retry.resolved",
            {
                "recorded_at": resolved_at,
                "target_stage": target_stage,
                "resolution": resolution,
                "actor": actor,
                "signal_id": signal_id,
                "resolved_count": resolved_count,
            },
        )
        if control.get("retry_exhausted_stage") == target_stage:
            control.pop("retry_exhausted", None)
            control.pop("retry_exhausted_stage", None)
        run = control.get("run")
        stage_plan = run.get("stage_plan") if isinstance(run, dict) else None
        if isinstance(stage_plan, list):
            for item in stage_plan:
                if isinstance(item, dict) and item.get("stage") == target_stage:
                    item["retry_status"] = resolution
                    item["last_retry_resolved_at"] = resolved_at
                    item["last_retry_resolved_by"] = actor
        return control

    def _resolve_specific_stage_retry(
        self,
        control: dict,
        retry_id: str,
        target_stage: str,
        resolution: str,
        resolved_at: str,
        actor: str,
        signal_id: str,
        eligible_statuses: set[str] | None = None,
        comment: str | None = None,
    ) -> dict:
        retry_queue = control.get("stage_retry_queue", [])
        if not isinstance(retry_queue, list):
            return control
        statuses = eligible_statuses or {"scheduled"}
        resolved = False
        updated_queue = []
        for item in retry_queue:
            if not isinstance(item, dict):
                updated_queue.append(item)
                continue
            if item.get("retry_id") == retry_id and item.get("status") in statuses:
                previous_status = item.get("status")
                item = {
                    **item,
                    "status": resolution,
                    "previous_status": previous_status,
                    "resolved_at": resolved_at,
                    "resolved_by": actor,
                    "resolution_signal_id": signal_id,
                    "next_retry_not_before": None,
                }
                if comment is not None:
                    item["resolution_comment"] = comment
                resolved = True
            updated_queue.append(item)
        if not resolved:
            return control
        control["stage_retry_queue"] = updated_queue
        control["last_stage_retry_resolution"] = {
            "schema_version": "workflow_stage_retry_resolution.v1",
            "target_stage": target_stage,
            "resolution": resolution,
            "resolved_at": resolved_at,
            "resolved_by": actor,
            "resolution_signal_id": signal_id,
            "resolved_count": 1,
            "retry_id": retry_id,
        }
        control = self._append_workflow_event(
            control,
            "workflow.stage_retry.resolved",
            {
                "recorded_at": resolved_at,
                "target_stage": target_stage,
                "resolution": resolution,
                "actor": actor,
                "signal_id": signal_id,
                "resolved_count": 1,
                "retry_id": retry_id,
            },
        )
        if control.get("retry_exhausted_stage") == target_stage:
            control.pop("retry_exhausted", None)
            control.pop("retry_exhausted_stage", None)
        run = control.get("run")
        stage_plan = run.get("stage_plan") if isinstance(run, dict) else None
        if isinstance(stage_plan, list):
            for item in stage_plan:
                if isinstance(item, dict) and item.get("stage") == target_stage:
                    item["retry_status"] = resolution
                    item["last_retry_resolved_at"] = resolved_at
                    item["last_retry_resolved_by"] = actor
        return control

    def _append_workflow_signal(
        self,
        control: dict,
        request: WorkflowActionRequest,
        signal_type: str,
        stage: str,
        extra: dict | None = None,
    ) -> tuple[dict, dict]:
        signal = {
            "signal_id": str(uuid4()),
            "signal_type": signal_type,
            "received_at": datetime.now(UTC).isoformat(),
            "stage": stage,
            "actor": request.user_id or "system",
            "comment": request.comment,
            **(extra or {}),
        }
        run = control.get("run")
        if not isinstance(run, dict):
            return control, signal
        signals = list(run.get("signals", []))
        signals.append(signal)
        run["signals"] = signals
        run["updated_at"] = signal["received_at"]
        control["run"] = run
        control = self._append_workflow_event(
            control,
            "workflow.signal.received",
            {
                "recorded_at": signal["received_at"],
                "signal_id": signal["signal_id"],
                "signal_type": signal_type,
                "stage": stage,
                "actor": signal["actor"],
                "comment": signal["comment"],
                **(extra or {}),
            },
        )
        return control, signal

    def _append_workflow_event(
        self,
        control: dict,
        event_type: str,
        details: dict,
    ) -> dict:
        run = control.get("run")
        if not isinstance(run, dict):
            return control
        event_log = list(control.get("workflow_event_log", []))
        sequence = len(event_log) + 1
        event_log.append(
            {
                "schema_version": "workflow_event.v1",
                "event_id": str(uuid4()),
                "event_sequence": sequence,
                "event_type": event_type,
                "recorded_at": details.get("recorded_at") or datetime.now(UTC).isoformat(),
                "run_id": details.get("run_id") or run.get("run_id"),
                "run_sequence": details.get("run_sequence") or run.get("run_sequence"),
                **{key: value for key, value in details.items() if key != "recorded_at"},
            }
        )
        control["workflow_event_log"] = event_log
        return control

    def _append_temporal_signal_log(
        self,
        episode: Episode,
        control: dict,
        signal: dict,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        run = control.get("run")
        run_id = run.get("run_id") if isinstance(run, dict) else None
        run_sequence = run.get("run_sequence") if isinstance(run, dict) else None
        entry = {
            "schema_version": "temporal_signal_transport_attempt.v1",
            "policy": self.external_temporal_policy,
            "signal_id": signal.get("signal_id"),
            "signal_type": signal.get("signal_type"),
            "episode_id": str(episode.id),
            "run_id": run_id,
            "run_sequence": run_sequence,
            "stage": signal.get("stage"),
            "actor": signal.get("actor"),
            "attempted_at": now,
            "enabled": self.settings.temporal_signal_transport_enabled,
            "endpoint_configured": bool(self.settings.temporal_signal_endpoint),
            "namespace": self.settings.temporal_namespace,
            "task_queue": self.settings.temporal_task_queue,
        }
        if isinstance(signal.get("manual_edit_evidence"), dict):
            entry["manual_edit_evidence"] = signal["manual_edit_evidence"]
        if not self.settings.temporal_signal_transport_enabled:
            entry["status"] = "disabled"
        elif not self.settings.temporal_signal_endpoint:
            entry.update(
                {
                    "status": "skipped",
                    "reason": "temporal signal endpoint is not configured",
                }
            )
        else:
            payload = {
                "schema_version": "temporal_signal_request.v1",
                "policy": self.external_temporal_policy,
                "episode_id": str(episode.id),
                "run_id": run_id,
                "run_sequence": run_sequence,
                "signal": signal,
                "namespace": self.settings.temporal_namespace,
                "task_queue": self.settings.temporal_task_queue,
            }
            try:
                response = self._post_temporal_signal(payload)
                entry.update(
                    {
                        "status": "sent",
                        "response_status_code": response.status_code,
                    }
                )
            except Exception as exc:
                entry.update(
                    {
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        signal_log = list(control.get("temporal_signal_log", []))
        signal_log.append(entry)
        control["temporal_signal_log"] = signal_log
        return control

    def _post_temporal_signal(self, payload: dict) -> httpx.Response:
        response = httpx.post(
            str(self.settings.temporal_signal_endpoint),
            json=payload,
            timeout=self.settings.temporal_signal_timeout_seconds,
        )
        response.raise_for_status()
        return response

    def _temporal_summary(self) -> dict:
        return {
            "policy": self.external_temporal_policy,
            "signal_transport_enabled": self.settings.temporal_signal_transport_enabled,
            "endpoint_configured": bool(self.settings.temporal_signal_endpoint),
            "workflow_replay": "local_event_journal",
            "namespace": self.settings.temporal_namespace,
            "task_queue": self.settings.temporal_task_queue,
        }

    def _replay_events(self, event_log: list[dict]) -> dict:
        replayed = {
            "run_id": None,
            "run_sequence": None,
            "state": None,
            "current_stage": None,
            "completion_reason": None,
            "stage_history": [],
            "signal_count": 0,
            "signals": [],
        }
        for event in sorted(event_log, key=lambda item: int(item.get("event_sequence") or 0)):
            event_type = event.get("event_type")
            if event_type == "workflow.run.started":
                replayed.update(
                    {
                        "run_id": event.get("run_id"),
                        "run_sequence": event.get("run_sequence"),
                        "state": event.get("state") or "running",
                        "current_stage": event.get("stage"),
                    }
                )
                replayed["stage_history"].append(
                    {
                        "stage": event.get("stage"),
                        "entered_at": event.get("recorded_at"),
                        "source": event.get("source"),
                    }
                )
            elif event_type == "workflow.stage.entered":
                replayed["current_stage"] = event.get("stage")
                replayed["stage_history"].append(
                    {
                        "stage": event.get("stage"),
                        "entered_at": event.get("recorded_at"),
                        "source": event.get("source"),
                    }
                )
            elif event_type == "workflow.signal.received":
                replayed["signal_count"] += 1
                replayed["signals"].append(self._workflow_signal_projection(event))
                if event.get("signal_type") in {
                    "retry_failed_stage",
                    "continue_after_manual_edit",
                }:
                    replayed["state"] = "running"
                    replayed["current_stage"] = event.get("stage")
                    replayed["completion_reason"] = None
            elif event_type == "workflow.run.completed":
                replayed["state"] = event.get("state")
                replayed["current_stage"] = event.get("stage") or replayed["current_stage"]
                replayed["completion_reason"] = event.get("completion_reason")
        return replayed

    def _current_run_projection(self, run: object) -> dict:
        if not isinstance(run, dict):
            return {}
        stage_history = [
            {
                "stage": item.get("stage"),
                "entered_at": item.get("entered_at"),
                "source": item.get("source"),
            }
            for item in run.get("stage_history", [])
            if isinstance(item, dict)
        ]
        signals = [
            self._workflow_signal_projection(item)
            for item in run.get("signals", [])
            if isinstance(item, dict)
        ]
        return {
            "run_id": run.get("run_id"),
            "run_sequence": run.get("run_sequence"),
            "state": run.get("state"),
            "current_stage": run.get("current_stage"),
            "completion_reason": run.get("completion_reason"),
            "stage_history": stage_history,
            "signal_count": len(signals),
            "signals": signals,
        }

    def _workflow_signal_projection(self, signal: dict) -> dict:
        projection = {
            "signal_type": signal.get("signal_type"),
            "stage": signal.get("stage"),
            "actor": signal.get("actor"),
        }
        evidence = signal.get("manual_edit_evidence")
        if isinstance(evidence, dict):
            projected_evidence = {
                "schema_version": evidence.get("schema_version"),
                "event_count": evidence.get("event_count"),
                "by_event_type": evidence.get("by_event_type"),
                "evidence_checksum": evidence.get("evidence_checksum"),
            }
            projection["manual_edit_evidence"] = {
                key: value for key, value in projected_evidence.items() if value is not None
            }
        return projection

    def _replay_issues(self, event_log: list[dict], replayed: dict, current: dict) -> list[dict]:
        issues = []
        if not event_log:
            issues.append({"issue": "workflow_event_log_missing"})
            return issues
        expected_sequence = 1
        for event in event_log:
            if event.get("event_sequence") != expected_sequence:
                issues.append(
                    {
                        "issue": "workflow_event_sequence_gap",
                        "expected_sequence": expected_sequence,
                        "actual_sequence": event.get("event_sequence"),
                    }
                )
                expected_sequence = int(event.get("event_sequence") or expected_sequence)
            expected_sequence += 1
        for key in {"run_id", "run_sequence", "state", "current_stage", "completion_reason"}:
            if replayed.get(key) != current.get(key):
                issues.append(
                    {
                        "issue": "workflow_replay_mismatch",
                        "field": key,
                        "replayed": replayed.get(key),
                        "current": current.get(key),
                    }
                )
        replayed_stages = [item.get("stage") for item in replayed.get("stage_history", [])]
        current_stages = [item.get("stage") for item in current.get("stage_history", [])]
        if replayed_stages != current_stages:
            issues.append(
                {
                    "issue": "workflow_replay_mismatch",
                    "field": "stage_history",
                    "replayed": replayed_stages,
                    "current": current_stages,
                }
            )
        if replayed.get("signal_count") != current.get("signal_count"):
            issues.append(
                {
                    "issue": "workflow_replay_mismatch",
                    "field": "signal_count",
                    "replayed": replayed.get("signal_count"),
                    "current": current.get("signal_count"),
                }
            )
        if replayed.get("signals") != current.get("signals"):
            issues.append(
                {
                    "issue": "workflow_replay_mismatch",
                    "field": "signals",
                    "replayed": replayed.get("signals"),
                    "current": current.get("signals"),
                }
            )
        return issues

    def _event_log_checksum(self, event_log: list[dict]) -> str:
        payload = json.dumps(event_log, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _stable_checksum(self, payload: object) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def _has_failed_assets(self, episode: Episode) -> bool:
        return any(asset.status == "failed" for asset in episode.assets)

    def _append_audit(
        self,
        episode: Episode,
        event_type: str,
        request: WorkflowActionRequest,
        details: dict,
    ) -> None:
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type=event_type,
                actor=request.user_id or "system",
                details={
                    **details,
                    "comment": request.comment,
                },
            )
        )

    def _touch(self, episode: Episode) -> Episode:
        episode.updated_at = datetime.now(UTC)
        return episode

    def _worker_orchestration_summary_id(self, summary: dict) -> str:
        attempt_id = summary.get("orchestration_attempt_id")
        if isinstance(attempt_id, str) and attempt_id.strip():
            return attempt_id.strip()
        return str(uuid4())

    def _worker_orchestration_already_recorded(self, log: list, summary_id: str) -> bool:
        return any(
            isinstance(entry, dict) and entry.get("summary_id") == summary_id
            for entry in log
        )
