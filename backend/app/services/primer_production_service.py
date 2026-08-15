from __future__ import annotations

# ruff: noqa: E501, I001

import asyncio
import base64
import copy
import json
import hashlib
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from num2words import num2words
from app.core.config import Settings
from app.domain.defaults import default_render_presets
from app.domain.enums import AssetType, TranscriptType
from app.domain.schemas import (
    Asset,
    AuditEvent,
    Episode,
    PrimerNarratorProfile,
    PrimerNarrationTimingRequest,
    PrimerProductionRequest,
    PrimerProductionStatus,
    PrimerSpokenScriptApprovalRequest,
    PrimerSpokenScriptPrepareRequest,
    PrimerSpokenScriptReplacement,
    PrimerSpokenScriptStatus,
    PrimerSpokenScriptUpdateRequest,
    PrimerVisualPlanApprovalRequest,
    PrimerVisualPlanBeatCreateRequest,
    PrimerVisualPlanBeatUpdateRequest,
    PrimerVisualPlanPrepareRequest,
    PrimerVisualPlanRevisionList,
    PrimerVisualPlanRevisionSummary,
    PrimerVisualPlanStatus,
    PrimerVisualPlanVerificationRequest,
    TranscriptTurn,
    TranscriptVersion,
    VoiceboxEndpoint,
    VoiceProfile,
)
from app.services.model_gateway import SecretResolver, auth_headers, openrouter_reasoning_parameters
from app.services.object_storage import create_object_store
from app.services.render_service import RenderService
from app.services.voicebox_service import VoiceboxService


class PrimerProductionService:
    """Produce a self-contained, off-camera primer without touching panel turns."""

    def __init__(
        self,
        settings: Settings,
        voicebox: VoiceboxService,
        render: RenderService,
    ) -> None:
        self.settings = settings
        self.voicebox = voicebox
        self.render = render
        self.object_store = create_object_store(settings)
        self.secret_resolver = SecretResolver()

    def status(
        self,
        episode: Episode,
        narrator: PrimerNarratorProfile | None = None,
    ) -> PrimerProductionStatus:
        state = self._state(episode)
        render_asset_id = state.get("render_asset_id")
        return PrimerProductionStatus(
            episode_id=episode.id,
            status=str(state.get("status") or "not_started"),
            narrator_profile_id=state.get("narrator_profile_id"),
            target_duration_seconds=int(state.get("target_duration_seconds") or 0),
            script=str(state.get("script") or ""),
            source_count=int(state.get("source_count") or 0),
            media_asset_count=int(state.get("media_asset_count") or 0),
            narration_asset_id=self._uuid_or_none(state.get("narration_asset_id")),
            actual_narration_duration_ms=self._optional_int(
                state.get("actual_narration_duration_ms")
            ),
            narration_timing=(
                state.get("narration_timing")
                if isinstance(state.get("narration_timing"), dict)
                else None
            ),
            timeline_asset_id=self._uuid_or_none(state.get("timeline_asset_id")),
            render_asset_id=self._uuid_or_none(render_asset_id),
            render_download_url=(
                f"/api/v1/episodes/{episode.id}/assets/{render_asset_id}/download"
                if render_asset_id
                else None
            ),
            editorial_polish=state.get("editorial_polish"),
            narration_quality=state.get("narration_quality"),
            spoken_script=self.spoken_script_status(episode, narrator),
            visual_plan=self.visual_plan_status(episode),
            failure=state.get("failure"),
        )

    def visual_plan_status(self, episode: Episode) -> PrimerVisualPlanStatus:
        state = self._visual_plan_state(episode)
        beats = state.get("beats") if isinstance(state.get("beats"), list) else []
        status = str(state.get("status") or "not_prepared")
        if status not in {"not_prepared", "review_required", "blocked", "approved"}:
            status = "not_prepared"
        coverage = self._visual_plan_coverage(episode, beats) if beats else {}
        if beats and status != "approved":
            status = "review_required" if coverage.get("ready") else "blocked"
        if status == "approved" and not coverage.get("ready"):
            status = "review_required"
        failure = None if coverage.get("ready") else "; ".join(coverage.get("blockers", []))
        return PrimerVisualPlanStatus(
            episode_id=episode.id,
            status=status,
            script=str(state.get("script") or ""),
            script_checksum=str(state.get("script_checksum") or "") or None,
            beat_count=len(beats),
            video_beat_count=int(coverage.get("video_beat_count") or 0),
            distinct_video_asset_count=int(coverage.get("distinct_video_asset_count") or 0),
            video_coverage_ratio=float(coverage.get("video_coverage_ratio") or 0.0),
            coverage=coverage,
            beats=beats,
            planner=state.get("planner") if isinstance(state.get("planner"), dict) else None,
            narration_timing=(
                state.get("narration_timing")
                if isinstance(state.get("narration_timing"), dict)
                else None
            ),
            failure=failure or (str(state.get("failure") or "") or None),
        )

    def spoken_script_status(
        self,
        episode: Episode,
        narrator: PrimerNarratorProfile | None = None,
    ) -> PrimerSpokenScriptStatus:
        state = self._state(episode)
        editorial_script = self._normalise_spoken_prose(str(state.get("script") or ""))
        editorial_checksum = (
            hashlib.sha256(editorial_script.encode()).hexdigest() if editorial_script else None
        )
        if narrator is not None and not narrator.pronunciation.enabled:
            return PrimerSpokenScriptStatus(
                status="not_required",
                editorial_script=editorial_script,
                editorial_script_checksum=editorial_checksum,
                spoken_script=editorial_script,
                spoken_script_checksum=editorial_checksum,
                profile_fingerprint=self._pronunciation_profile_fingerprint(narrator),
            )
        raw = state.get("spoken_script")
        if not isinstance(raw, dict):
            return PrimerSpokenScriptStatus(
                status="not_prepared",
                editorial_script=editorial_script,
                editorial_script_checksum=editorial_checksum,
            )
        payload = copy.deepcopy(raw)
        payload["editorial_script"] = editorial_script
        stored_status = str(payload.get("status") or "not_prepared")
        if editorial_checksum and payload.get("editorial_script_checksum") != editorial_checksum:
            stored_status = "outdated"
        if narrator is not None:
            fingerprint = self._pronunciation_profile_fingerprint(narrator)
            if payload.get("profile_fingerprint") != fingerprint:
                stored_status = "outdated"
        payload["status"] = stored_status
        try:
            return PrimerSpokenScriptStatus.model_validate(payload)
        except ValueError:
            return PrimerSpokenScriptStatus(
                status="blocked",
                editorial_script=editorial_script,
                editorial_script_checksum=editorial_checksum,
                failure="stored spoken narration state is invalid",
            )

    async def prepare_spoken_script(
        self,
        episode: Episode,
        request: PrimerSpokenScriptPrepareRequest,
        narrator: PrimerNarratorProfile,
        model_endpoints: list,
    ) -> PrimerSpokenScriptStatus:
        editorial_script = self._approved_editorial_script(episode)
        if not narrator.pronunciation.enabled:
            return self.spoken_script_status(episode, narrator)
        replacements = self._deterministic_pronunciation_replacements(
            editorial_script,
            narrator,
        )
        replacements = self._validated_pronunciation_replacements(
            editorial_script,
            replacements,
        )
        ai_assistance: dict = {
            "status": "disabled",
            "reason": "ai_pronunciation_assistance_disabled",
        }
        punctuation_script: str | None = None
        if narrator.pronunciation.use_ai:
            endpoint_id = (
                narrator.pronunciation.model_endpoint_id or narrator.model_endpoint_id
            )
            endpoint = next((item for item in model_endpoints if item.id == endpoint_id), None)
            model_id = narrator.pronunciation.model_id or narrator.model_id
            if endpoint is None or not endpoint.enabled or not endpoint.base_url:
                ai_assistance = {
                    "status": "unavailable",
                    "reason": "pronunciation_model_endpoint_unavailable",
                    "model_endpoint_id": endpoint_id,
                    "model_id": model_id,
                }
            else:
                try:
                    suggestions = await self._request_pronunciation_suggestions(
                        endpoint,
                        model_id,
                        editorial_script,
                        replacements,
                        narrator,
                    )
                    replacements = self._validated_pronunciation_replacements(
                        editorial_script,
                        [*replacements, *suggestions["replacements"]],
                    )
                    punctuation_script = suggestions.get("spoken_script")
                    ai_assistance = {
                        "status": "applied",
                        "model_endpoint_id": endpoint.id,
                        "model_id": model_id,
                        "suggested_replacement_count": len(
                            suggestions["replacements"]
                        ),
                    }
                except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    ai_assistance = {
                        "status": "unavailable",
                        "reason": "pronunciation_ai_output_failed_validation",
                        "error_type": type(exc).__name__,
                        "model_endpoint_id": endpoint.id,
                        "model_id": model_id,
                    }
        server_candidate = self._apply_pronunciation_replacements(
            editorial_script,
            replacements,
        )
        spoken_script = server_candidate
        if punctuation_script:
            punctuation_script = self._normalise_spoken_prose(punctuation_script)
            if self._spoken_token_signature(punctuation_script) == self._spoken_token_signature(
                server_candidate
            ):
                spoken_script = punctuation_script
            else:
                ai_assistance = {
                    **ai_assistance,
                    "status": "partial",
                    "reason": "ai_punctuation_changed_spoken_tokens",
                }
        now = datetime.now(UTC)
        approved = not narrator.pronunciation.require_review
        spoken_state = {
            "status": "approved" if approved else "review_required",
            "editorial_script_checksum": hashlib.sha256(
                editorial_script.encode()
            ).hexdigest(),
            "spoken_script": spoken_script,
            "spoken_script_checksum": hashlib.sha256(spoken_script.encode()).hexdigest(),
            "profile_fingerprint": self._pronunciation_profile_fingerprint(narrator),
            "replacements": [item.model_dump(mode="json") for item in replacements],
            "ai_assistance": ai_assistance,
            "prepared_at": now.isoformat(),
            "approved_at": now.isoformat() if approved else None,
            "approved_by": (request.user_id or "automatic") if approved else None,
            "failure": None,
        }
        self._state(episode)["spoken_script"] = spoken_state
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.spoken_script.prepared",
                actor=request.user_id or "web-ui",
                details={
                    "status": spoken_state["status"],
                    "replacement_count": len(replacements),
                    "ai_status": ai_assistance.get("status"),
                    "spoken_script_checksum": spoken_state["spoken_script_checksum"],
                },
            )
        )
        return self.spoken_script_status(episode, narrator)

    def update_spoken_script(
        self,
        episode: Episode,
        request: PrimerSpokenScriptUpdateRequest,
        narrator: PrimerNarratorProfile,
    ) -> PrimerSpokenScriptStatus:
        editorial_script = self._approved_editorial_script(episode)
        if not narrator.pronunciation.enabled:
            raise ValueError("pronunciation preparation is disabled for the selected narrator")
        replacements = self._validated_pronunciation_replacements(
            editorial_script,
            [
                item.model_copy(update={"origin": "editor"})
                for item in request.replacements
            ],
        )
        server_candidate = self._apply_pronunciation_replacements(
            editorial_script,
            replacements,
        )
        spoken_script = server_candidate
        if request.punctuation_script:
            candidate = self._normalise_spoken_prose(request.punctuation_script)
            if self._spoken_token_signature(candidate) != self._spoken_token_signature(
                server_candidate
            ):
                raise ValueError(
                    "spoken narration edits may change pronunciation and punctuation, "
                    "but cannot add, remove, or reorder transformed words"
                )
            spoken_script = candidate
        now = datetime.now(UTC)
        previous = self._state(episode).get("spoken_script")
        ai_assistance = (
            previous.get("ai_assistance")
            if isinstance(previous, dict) and isinstance(previous.get("ai_assistance"), dict)
            else None
        )
        self._state(episode)["spoken_script"] = {
            "status": "review_required",
            "editorial_script_checksum": hashlib.sha256(
                editorial_script.encode()
            ).hexdigest(),
            "spoken_script": spoken_script,
            "spoken_script_checksum": hashlib.sha256(spoken_script.encode()).hexdigest(),
            "profile_fingerprint": self._pronunciation_profile_fingerprint(narrator),
            "replacements": [item.model_dump(mode="json") for item in replacements],
            "ai_assistance": ai_assistance,
            "prepared_at": now.isoformat(),
            "approved_at": None,
            "approved_by": None,
            "failure": None,
        }
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.spoken_script.edited",
                actor=request.user_id or "web-ui",
                details={
                    "replacement_count": len(replacements),
                    "spoken_script_checksum": hashlib.sha256(
                        spoken_script.encode()
                    ).hexdigest(),
                },
            )
        )
        return self.spoken_script_status(episode, narrator)

    def approve_spoken_script(
        self,
        episode: Episode,
        request: PrimerSpokenScriptApprovalRequest,
        narrator: PrimerNarratorProfile,
    ) -> PrimerSpokenScriptStatus:
        status = self.spoken_script_status(episode, narrator)
        if status.status not in {"review_required", "approved"}:
            raise ValueError("prepare a current spoken narration script before approval")
        editorial_script = self._approved_editorial_script(episode)
        replacements = self._validated_pronunciation_replacements(
            editorial_script,
            status.replacements,
        )
        baseline = self._apply_pronunciation_replacements(editorial_script, replacements)
        if self._spoken_token_signature(status.spoken_script) != self._spoken_token_signature(
            baseline
        ):
            raise ValueError("spoken narration failed the content-equivalence gate")
        spoken_state = self._state(episode).get("spoken_script")
        if not isinstance(spoken_state, dict):
            raise ValueError("spoken narration state is missing")
        spoken_state.update(
            {
                "status": "approved",
                "approved_at": datetime.now(UTC).isoformat(),
                "approved_by": request.user_id or "web-ui",
                "failure": None,
            }
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.spoken_script.approved",
                actor=request.user_id or "web-ui",
                details={
                    "spoken_script_checksum": status.spoken_script_checksum,
                    "replacement_count": len(replacements),
                },
            )
        )
        return self.spoken_script_status(episode, narrator)

    def visual_plan_revisions(self, episode: Episode) -> PrimerVisualPlanRevisionList:
        revisions = self._visual_plan_revisions_state(episode)
        summaries: list[PrimerVisualPlanRevisionSummary] = []
        for revision in reversed(revisions):
            if not isinstance(revision, dict):
                continue
            summaries.append(
                PrimerVisualPlanRevisionSummary(
                    id=str(revision.get("id") or ""),
                    created_at=str(revision.get("created_at") or ""),
                    reason=str(revision.get("reason") or "visual plan change"),
                    actor=str(revision.get("actor") or "web-ui"),
                    status=str(revision.get("status") or "not_prepared"),
                    beat_count=max(0, int(revision.get("beat_count") or 0)),
                )
            )
        return PrimerVisualPlanRevisionList(episode_id=episode.id, revisions=summaries)

    def restore_visual_plan_revision(
        self,
        episode: Episode,
        revision_id: str,
        user_id: str | None = None,
    ) -> PrimerVisualPlanStatus:
        revisions = self._visual_plan_revisions_state(episode)
        revision = next(
            (
                item
                for item in revisions
                if isinstance(item, dict) and str(item.get("id") or "") == revision_id
            ),
            None,
        )
        if revision is None:
            raise KeyError("primer visual-plan revision not found")
        snapshot = revision.get("plan")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("beats"), list):
            raise ValueError("primer visual-plan revision has no restorable sequence")

        actor = user_id or "web-ui"
        self._archive_visual_plan(
            episode,
            reason=f"before restoring {revision_id}",
            actor=actor,
        )
        state = self._visual_plan_state(episode)
        state.clear()
        state.update(copy.deepcopy(snapshot))
        beats = state["beats"]
        coverage = self._visual_plan_coverage(episode, beats)
        restored_status = str(state.get("status") or "review_required")
        state.update(
            {
                "coverage": coverage,
                "status": "approved"
                if restored_status == "approved" and coverage["ready"]
                else "review_required"
                if coverage["ready"]
                else "blocked",
                "failure": None if coverage["ready"] else "; ".join(coverage["blockers"]),
                "restored_at": datetime.now(UTC).isoformat(),
                "restored_from_revision_id": revision_id,
            }
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.visual_plan.revision.restored",
                actor=actor,
                details={
                    "revision_id": revision_id,
                    "beat_count": len(beats),
                    "coverage": coverage,
                },
            )
        )
        return self.visual_plan_status(episode)

    async def prepare_visual_plan(
        self,
        episode: Episode,
        request: PrimerVisualPlanPrepareRequest,
        narrator: PrimerNarratorProfile,
        model_endpoints: list,
    ) -> PrimerVisualPlanStatus:
        if not narrator.enabled:
            raise ValueError("selected narrator profile is disabled")
        if narrator.language != episode.source_language:
            raise ValueError("narrator language must match the episode source language")
        sources = self._evidence_sources(episode)
        if not sources:
            raise ValueError("primer visual planning requires source evidence")
        existing_visual_plan = self._visual_plan_state(episode)
        if (
            isinstance(existing_visual_plan.get("beats"), list)
            and existing_visual_plan["beats"]
            and not request.replace_existing_plan
        ):
            raise ValueError(
                "refusing to replace the existing primer visual plan without explicit confirmation"
            )
        endpoint = next(
            (item for item in model_endpoints if item.id == narrator.model_endpoint_id), None
        )

        production_state = self._state(episode)
        target_duration_seconds = episode.definition.media.opening.target_duration_seconds
        existing_script = self._normalise_spoken_prose(str(production_state.get("script") or ""))
        script_override = self._normalise_spoken_prose(request.script_override or "")
        if script_override and not self._is_usable_polished_script(
            script_override, self._target_word_count(target_duration_seconds)
        ):
            raise ValueError("script override does not meet the primer spoken-prose quality policy")
        if request.reuse_existing_script and not existing_script:
            raise ValueError("no existing primer script is available to plan")
        if request.reuse_existing_script and not self._is_reusable_editorial_script(
            existing_script,
            production_state.get("editorial_polish"),
            self._target_word_count(target_duration_seconds),
        ):
            raise ValueError(
                "the current primer script was not approved as fluent spoken narration; "
                "draft a new narration instead"
            )
        if script_override:
            script = script_override
            editorial_polish = {
                "status": "retained",
                "reason": "operator_supplied_prior_approved_script",
                "word_count": len(script.split()),
            }
        elif request.reuse_existing_script:
            script = existing_script
            editorial_polish = {
                "status": "retained",
                "reason": "visual_plan_reuses_existing_script",
                "word_count": len(script.split()),
            }
        else:
            if endpoint is None or not endpoint.enabled:
                raise ValueError("narrator model endpoint is not available")
            try:
                draft = await self._draft_script(
                    episode, narrator, endpoint, sources, target_duration_seconds
                )
                script, editorial_polish = await self._polish_script(
                    episode,
                    narrator,
                    endpoint,
                    sources,
                    target_duration_seconds,
                    draft,
                )
            except ValueError as exc:
                previous_polish = production_state.get("editorial_polish")
                production_state.update(
                    {
                        "schema_version": "dialecticore.primer_production.v1",
                        "status": "narration_draft_failed",
                        "narrator_profile_id": narrator.id,
                        "target_duration_seconds": target_duration_seconds,
                        "source_count": len(sources),
                        "editorial_polish": {
                            "status": "blocked",
                            "reason": "editorial_narration_quality_gate",
                            "failure": str(exc),
                        },
                        "failure": str(exc),
                    }
                )
                if isinstance(previous_polish, dict) and previous_polish.get("status") in {
                    "fallback",
                    "blocked",
                    "skipped",
                }:
                    production_state.pop("script", None)
                    production_state.pop("draft_script", None)
                episode.audit_events.append(
                    AuditEvent(
                        episode_id=episode.id,
                        event_type="primer.narration.draft_blocked",
                        actor=request.user_id or "web-ui",
                        details={"failure": str(exc), "narrator_profile_id": narrator.id},
                    )
                )
                raise

        production_state.update(
            {
                "schema_version": "dialecticore.primer_production.v1",
                "status": "visual_plan_review",
                "narrator_profile_id": narrator.id,
                "narrator_profile_snapshot": narrator.model_dump(mode="json"),
                "target_duration_seconds": target_duration_seconds,
                "source_count": len(sources),
                "script": script,
                "draft_script": script,
                "editorial_polish": editorial_polish,
                "failure": None,
            }
        )
        visual_assets = self._opening_visual_assets(episode)
        planner = self._visual_planner_config(episode)
        still_safety = await self._assess_visual_suitability(
            visual_assets=[asset for asset in visual_assets if asset.asset_type == AssetType.image],
            planner=planner,
            model_endpoints=model_endpoints,
        )
        eligible_assets = self._people_free_visual_assets(visual_assets, planner)
        plan = self._build_visual_plan(
            episode=episode,
            script=script,
            target_duration_seconds=target_duration_seconds,
            visual_assets=eligible_assets,
        )
        planner_result = await self._apply_ai_storyboard_plan(
            episode=episode,
            script=script,
            visual_assets=eligible_assets,
            beats=plan["beats"],
            planner=planner,
            model_endpoints=model_endpoints,
        )
        self._assign_video_source_ranges(plan["beats"], eligible_assets)
        video_safety = await self._verify_people_free_video_windows(
            beats=plan["beats"],
            visual_assets=eligible_assets,
            planner=planner,
            model_endpoints=model_endpoints,
        )
        plan["planner"] = {
            **planner_result,
            "source_video_coverage_policy": {
                "status": "not_applied",
                "reason": "editor_controlled_visual_sequence",
            },
            "people_free_policy": {
                "still_images": still_safety,
                "video_windows": video_safety,
            },
        }
        plan["coverage"] = self._visual_plan_coverage(episode, plan["beats"])
        plan["status"] = "review_required" if plan["coverage"]["ready"] else "blocked"
        plan["failure"] = (
            None if plan["coverage"]["ready"] else "; ".join(plan["coverage"]["blockers"])
        )
        visual_state = self._visual_plan_state(episode)
        self._archive_visual_plan(
            episode,
            reason="before preparing a new AI visual plan",
            actor=request.user_id or "web-ui",
        )
        visual_state.clear()
        visual_state.update(plan)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.visual_plan.prepared",
                actor=request.user_id or "web-ui",
                details={
                    "beat_count": len(plan["beats"]),
                    "status": plan["status"],
                    "coverage": plan["coverage"],
                    "script_checksum": plan["script_checksum"],
                },
            )
        )
        return self.visual_plan_status(episode)

    async def prepare_narration_timing(
        self,
        episode: Episode,
        request: PrimerNarrationTimingRequest,
        narrator: PrimerNarratorProfile,
        voice_profiles: list[VoiceProfile],
        voicebox_endpoints: list[VoiceboxEndpoint],
    ) -> PrimerProductionStatus:
        """Measure the actual narrator WAV before source excerpts are approved."""
        if not narrator.enabled:
            raise ValueError("selected narrator profile is disabled")
        if narrator.language != episode.source_language:
            raise ValueError("narrator language must match the episode source language")

        state = self._state(episode)
        script = self._normalise_spoken_prose(str(state.get("script") or ""))
        target_seconds = episode.definition.media.opening.target_duration_seconds
        if not self._is_reusable_editorial_script(
            script,
            state.get("editorial_polish"),
            self._target_word_count(target_seconds),
        ):
            raise ValueError(
                "prepare an approved primer narration before generating its timing track"
            )
        spoken_script = self._approved_spoken_script(episode, narrator, script)

        narration = self._reusable_narration_asset(
            episode,
            state,
            spoken_script,
            narrator,
            reuse_requested=not request.regenerate,
        )
        generated = narration is None
        if narration is None:
            narration = await self._narrate(
                episode,
                spoken_script,
                narrator,
                voice_profiles,
                voicebox_endpoints,
                editorial_script=script,
            )
            episode.assets.append(narration)

        duration_ms = int(narration.duration_ms or 0)
        if duration_ms <= 0:
            raise ValueError("narrator audio has no measurable duration")
        state.update(
            {
                "narration_asset_id": str(narration.id),
                "actual_narration_duration_ms": duration_ms,
                "narration_quality": narration.generation_metadata.get("transcription_qc"),
                "narrator_profile_id": narrator.id,
                "narrator_profile_snapshot": narrator.model_dump(mode="json"),
                "spoken_script_checksum": hashlib.sha256(
                    spoken_script.encode()
                ).hexdigest(),
                "failure": None,
            }
        )
        timing = self._synchronise_visual_plan_to_narration(
            episode,
            narration,
            script,
            actor=request.user_id or "web-ui",
        )
        state["narration_timing"] = timing
        if timing.get("review_required"):
            state["status"] = "visual_timing_review"
        elif state.get("status") in {"completed", "failed", "visual_timing_review"}:
            state["status"] = "visual_plan_review"
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.narration.timing.prepared",
                actor=request.user_id or "web-ui",
                details={
                    "narration_asset_id": str(narration.id),
                    "duration_ms": duration_ms,
                    "generated": generated,
                    "visual_retiming_required": bool(timing.get("review_required")),
                },
            )
        )
        return self.status(episode, narrator)

    def add_visual_plan_beat(
        self,
        episode: Episode,
        request: PrimerVisualPlanBeatCreateRequest,
    ) -> PrimerVisualPlanStatus:
        state = self._visual_plan_state(episode)
        beats = state.get("beats")
        if not isinstance(beats, list):
            raise ValueError("prepare a primer visual plan before adding a visual")
        insert_at = len(beats)
        if request.after_beat_id:
            after_index = next(
                (
                    index
                    for index, beat in enumerate(beats)
                    if isinstance(beat, dict) and beat.get("id") == request.after_beat_id
                ),
                None,
            )
            if after_index is None:
                raise ValueError("primer visual beat was not found")
            insert_at = after_index + 1
        planner = self._visual_planner_config(episode)
        target_duration_ms = self._visual_plan_target_duration_ms(episode, beats)
        opening_assets = {str(asset.id): asset for asset in self._opening_visual_assets(episode)}
        asset = opening_assets.get(str(request.asset_id)) if request.asset_id else None
        if request.asset_id and asset is None:
            raise ValueError("selected asset is not approved opening source media")
        if (
            asset is not None
            and planner["exclude_people"]
            and asset.asset_type == AssetType.image
            and not self._asset_people_free_status(asset)[0]
        ):
            raise ValueError(
                "selected source media is not verified people-free for this episode's primer policy"
            )
        self._archive_visual_plan(
            episode,
            reason="before adding a visual beat",
            actor=request.user_id or "web-ui",
        )
        beats.insert(
            insert_at,
            {
                "id": f"primer-beat-{uuid4().hex[:12]}",
                "start_ms": 0,
                "end_ms": 0,
                "duration_ms": planner["target_shot_duration_seconds"] * 1000,
                "target_narration_duration_ms": target_duration_ms,
                "purpose": "factual_context",
                "narration_excerpt": "",
                "asset_id": str(asset.id) if asset else None,
                "asset_type": asset.asset_type.value if asset else None,
                "source_start_ms": 0,
                "source_end_ms": None,
                "source_title": self._asset_source_title(asset) if asset else None,
                "source_url": asset.generation_metadata.get("source_url") if asset else None,
                "still_motion": "push_in",
                "camera_transition": "dissolve",
                "visual_intent": "editor-selected source visual",
                "selection_rationale": "manual sequence insertion",
                "selection_method": "manual_sequence_edit",
                "provenance": "source_media" if asset else "unassigned",
                "timing_source": (
                    "pending_manual_excerpt_verification"
                    if asset and asset.asset_type == AssetType.video
                    else "storyboard"
                ),
                "people_free_verification": (
                    {
                        "status": "not_verified",
                        "people_visible": None,
                        "reason": "manual_excerpt_range_required",
                    }
                    if asset and asset.asset_type == AssetType.video and planner["exclude_people"]
                    else None
                ),
                "review_status": "proposed",
            },
        )
        self._reflow_visual_plan_structure(episode, state, beats)
        self._refresh_visual_plan_review_state(
            episode,
            state,
            beats,
            event_type="primer.visual_plan.beat.added",
            actor=request.user_id or "web-ui",
            details={
                "after_beat_id": request.after_beat_id,
                "asset_id": str(asset.id) if asset else None,
            },
        )
        return self.visual_plan_status(episode)

    def remove_visual_plan_beat(
        self,
        episode: Episode,
        beat_id: str,
        *,
        user_id: str | None = None,
    ) -> PrimerVisualPlanStatus:
        state = self._visual_plan_state(episode)
        beats = state.get("beats")
        if not isinstance(beats, list):
            raise ValueError("prepare a primer visual plan before removing a visual")
        removed_index = next(
            (
                index
                for index, beat in enumerate(beats)
                if isinstance(beat, dict) and beat.get("id") == beat_id
            ),
            None,
        )
        if removed_index is None:
            raise ValueError("primer visual beat was not found")
        self._archive_visual_plan(
            episode,
            reason="before removing a visual beat",
            actor=user_id or "web-ui",
        )
        removed = beats.pop(removed_index)
        self._reflow_visual_plan_structure(episode, state, beats)
        self._refresh_visual_plan_review_state(
            episode,
            state,
            beats,
            event_type="primer.visual_plan.beat.removed",
            actor=user_id or "web-ui",
            details={
                "removed_beat_id": beat_id,
                "removed_asset_id": removed.get("asset_id") if isinstance(removed, dict) else None,
            },
        )
        return self.visual_plan_status(episode)

    def update_visual_plan_beat(
        self,
        episode: Episode,
        beat_id: str,
        request: PrimerVisualPlanBeatUpdateRequest,
    ) -> PrimerVisualPlanStatus:
        state = self._visual_plan_state(episode)
        beats = state.get("beats")
        if not isinstance(beats, list):
            raise ValueError("prepare a primer visual plan before assigning beats")
        opening_assets = {str(item.id): item for item in self._opening_visual_assets(episode)}
        asset = opening_assets.get(str(request.asset_id))
        if asset is None:
            raise ValueError("selected asset is not approved opening source media")
        planner = self._visual_planner_config(episode)
        if (
            planner["exclude_people"]
            and asset.asset_type == AssetType.image
            and not self._asset_people_free_status(asset)[0]
        ):
            raise ValueError(
                "selected source media is not verified people-free for this episode's primer policy"
            )
        beat = next((item for item in beats if isinstance(item, dict) and item.get("id") == beat_id), None)
        if beat is None:
            raise ValueError("primer visual beat was not found")
        self._archive_visual_plan(
            episode,
            reason="before updating a visual beat",
            actor=request.user_id or "web-ui",
        )
        selected_duration_ms = self._optional_int(beat.get("duration_ms")) or 0
        has_valid_video_range = (
            asset.asset_type == AssetType.video
            and request.source_end_ms is not None
            and request.source_end_ms > request.source_start_ms
        )
        if has_valid_video_range:
            selected_duration_ms = request.source_end_ms - request.source_start_ms
        beat.update(
            {
                "asset_id": str(asset.id),
                "asset_type": asset.asset_type.value,
                "source_start_ms": request.source_start_ms if asset.asset_type == AssetType.video else 0,
                "source_end_ms": request.source_end_ms if asset.asset_type == AssetType.video else None,
                # The selected duration drives the editor timeline immediately; verification
                # remains the gate for approval and rendering.
                "timing_source": (
                    "pending_manual_excerpt_verification"
                    if asset.asset_type == AssetType.video
                    else "storyboard"
                ),
                "duration_ms": selected_duration_ms,
                "manual_excerpt_duration_ms": (
                    selected_duration_ms if has_valid_video_range else None
                ),
                "source_title": asset.generation_metadata.get("source_title")
                or asset.generation_metadata.get("title")
                or "Source media",
                "source_url": asset.generation_metadata.get("source_url"),
                "people_free_verification": (
                    {
                        "status": "not_verified",
                        "reason": "source_range_changed_requires_people_free_review",
                    }
                    if asset.asset_type == AssetType.video and planner["exclude_people"]
                    else None
                ),
                "review_status": "assigned",
            }
        )
        self._reflow_visual_plan_structure(episode, state, beats)
        self._refresh_visual_plan_review_state(
            episode,
            state,
            beats,
            event_type="primer.visual_plan.beat.updated",
            actor=request.user_id or "web-ui",
            details={"beat_id": beat_id, "asset_id": str(asset.id)},
        )
        return self.visual_plan_status(episode)

    async def verify_visual_plan_excerpts(
        self,
        episode: Episode,
        request: PrimerVisualPlanVerificationRequest,
        model_endpoints: list,
    ) -> PrimerVisualPlanStatus:
        """Verify edited video ranges while preserving the producer's storyboard choices."""
        state = self._visual_plan_state(episode)
        beats = state.get("beats")
        if not isinstance(beats, list) or not beats:
            raise ValueError("prepare a primer visual plan before verifying selected excerpts")
        planner = self._visual_planner_config(episode)
        opening_assets = {str(asset.id): asset for asset in self._opening_visual_assets(episode)}
        pending_beats: list[dict] = []
        windows: list[dict] = []
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            asset = opening_assets.get(str(beat.get("asset_id") or ""))
            if asset is None or asset.asset_type != AssetType.video:
                continue
            verification = beat.get("people_free_verification")
            manual_timing_pending = (
                beat.get("timing_source") == "pending_manual_excerpt_verification"
            )
            people_free_verified = (
                isinstance(verification, dict)
                and verification.get("status") == "verified"
                and verification.get("people_visible") is False
            )
            if not manual_timing_pending and (
                not planner["exclude_people"] or people_free_verified
            ):
                continue
            start_ms = self._optional_int(beat.get("source_start_ms"))
            end_ms = self._optional_int(beat.get("source_end_ms"))
            if start_ms is None or end_ms is None or end_ms <= start_ms:
                beat["people_free_verification"] = {
                    "status": "not_verified",
                    "people_visible": None,
                    "reason": "manual_excerpt_range_invalid",
                }
                continue
            pending_beats.append(beat)
            windows.append(
                {
                    "window_id": f"manual-{beat.get('id')}",
                    "beat_id": str(beat.get("id") or ""),
                    "asset_id": str(asset.id),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "selection_method": "manual_trim",
                }
            )
        if planner["exclude_people"] and pending_beats:
            video_safety = await self._verify_people_free_video_windows(
                beats=pending_beats,
                visual_assets=list(opening_assets.values()),
                planner=planner,
                model_endpoints=model_endpoints,
                candidate_windows=windows,
                preserve_selected_ranges=True,
            )
        elif planner["exclude_people"]:
            video_safety = {
                "status": "reused",
                "reason": "no_unverified_video_excerpts",
                "candidate_window_count": 0,
                "verified_window_count": 0,
            }
        else:
            video_safety = {
                "status": "not_required",
                "reason": "people_allowed_by_episode_policy",
                "candidate_window_count": 0,
                "verified_window_count": 0,
            }
        self._archive_visual_plan(
            episode,
            reason="before verifying selected video excerpts",
            actor=request.user_id or "web-ui",
        )
        planner_state = state.get("planner") if isinstance(state.get("planner"), dict) else {}
        policy = (
            planner_state.get("people_free_policy")
            if isinstance(planner_state.get("people_free_policy"), dict)
            else {}
        )
        state["planner"] = {
            **planner_state,
            "people_free_policy": {**policy, "video_windows": video_safety},
        }
        timing_applied_count = self._apply_verified_manual_excerpt_timing(
            beats,
            require_people_free_verification=planner["exclude_people"],
        )
        if timing_applied_count:
            self._reflow_visual_plan_structure(episode, state, beats)
        narration_timing = state.get("narration_timing")
        if isinstance(narration_timing, dict) and narration_timing.get("review_required"):
            narration_timing.update(
                {
                    "review_required": False,
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "reviewed_by": request.user_id or "web-ui",
                }
            )
            for beat in beats:
                if isinstance(beat, dict):
                    beat.pop("narration_timing_review_required", None)
        coverage = self._visual_plan_coverage(episode, beats)
        state.update(
            {
                "coverage": coverage,
                "status": "review_required" if coverage["ready"] else "blocked",
                "failure": None if coverage["ready"] else "; ".join(coverage["blockers"]),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.visual_plan.excerpts.verified",
                actor=request.user_id or "web-ui",
                details={
                    "verified_candidate_count": len(windows),
                    "timing_applied_count": timing_applied_count,
                    "video_safety": video_safety,
                    "coverage": coverage,
                },
            )
        )
        return self.visual_plan_status(episode)

    async def assess_visual_plan_source_media(
        self,
        episode: Episode,
        request: PrimerVisualPlanVerificationRequest,
        model_endpoints: list,
    ) -> PrimerVisualPlanStatus:
        """Assess newly added stills without rebuilding the editor's visual sequence."""
        planner = self._visual_planner_config(episode)
        still_assets = [
            asset
            for asset in self._opening_visual_assets(episode)
            if asset.asset_type == AssetType.image
        ]
        assessment = await self._assess_visual_suitability(
            visual_assets=still_assets,
            planner=planner,
            model_endpoints=model_endpoints,
        )
        state = self._visual_plan_state(episode)
        planner_state = state.get("planner") if isinstance(state.get("planner"), dict) else {}
        policy = (
            planner_state.get("people_free_policy")
            if isinstance(planner_state.get("people_free_policy"), dict)
            else {}
        )
        self._archive_visual_plan(
            episode,
            reason="before assessing source-media suitability",
            actor=request.user_id or "web-ui",
        )
        state["planner"] = {
            **planner_state,
            "people_free_policy": {**policy, "still_images": assessment},
        }
        beats = state.get("beats") if isinstance(state.get("beats"), list) else []
        if beats:
            coverage = self._visual_plan_coverage(episode, beats)
            state["coverage"] = coverage
            if state.get("status") != "approved":
                state["status"] = "review_required" if coverage["ready"] else "blocked"
                state["failure"] = None if coverage["ready"] else "; ".join(coverage["blockers"])
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.visual_sources.assessed",
                actor=request.user_id or "web-ui",
                details={"still_image_assessment": assessment},
            )
        )
        return self.visual_plan_status(episode)

    def approve_visual_plan(
        self,
        episode: Episode,
        request: PrimerVisualPlanApprovalRequest,
    ) -> PrimerVisualPlanStatus:
        state = self._visual_plan_state(episode)
        beats = state.get("beats")
        if not isinstance(beats, list) or not beats:
            raise ValueError("prepare a primer visual plan before approval")
        timing = state.get("narration_timing")
        expected_script_checksum = hashlib.sha256(
            self._normalise_spoken_prose(str(self._state(episode).get("script") or "")).encode()
        ).hexdigest()
        if not isinstance(timing, dict) or timing.get("status") != "measured":
            raise ValueError(
                "generate narration timing before approving source-video excerpts"
            )
        if timing.get("script_checksum") != expected_script_checksum:
            raise ValueError(
                "the narration changed; generate timing again before approving the visual plan"
            )
        if timing.get("review_required"):
            raise ValueError(
                "the measured narration duration changed; review and verify the selected excerpts"
            )
        coverage = self._visual_plan_coverage(episode, beats)
        state["coverage"] = coverage
        if not coverage["ready"]:
            state["status"] = "blocked"
            state["failure"] = "; ".join(coverage["blockers"])
            raise ValueError("primer visual plan is incomplete: " + state["failure"])
        self._archive_visual_plan(
            episode,
            reason="before approving the visual plan",
            actor=request.user_id or "web-ui",
        )
        for beat in beats:
            if isinstance(beat, dict):
                beat["review_status"] = "approved"
        state.update(
            {
                "status": "approved",
                "failure": None,
                "approved_at": datetime.now(UTC).isoformat(),
                "approved_by": request.user_id or "web-ui",
            }
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.visual_plan.approved",
                actor=request.user_id or "web-ui",
                details={"beat_count": len(beats), "coverage": coverage},
            )
        )
        return self.visual_plan_status(episode)

    async def produce(
        self,
        episode: Episode,
        request: PrimerProductionRequest,
        narrator: PrimerNarratorProfile,
        model_endpoints: list,
        voice_profiles: list[VoiceProfile],
        voicebox_endpoints: list[VoiceboxEndpoint],
    ) -> PrimerProductionStatus:
        if not narrator.enabled:
            raise ValueError("selected narrator profile is disabled")
        if narrator.language != episode.source_language:
            raise ValueError("narrator language must match the episode source language")
        sources = self._evidence_sources(episode)
        if not sources:
            raise ValueError("primer production requires source evidence before narration")
        state = self._state(episode)
        if request.rebuild_visual_plan:
            existing_visual_plan = self._visual_plan_state(episode)
            if (
                isinstance(existing_visual_plan.get("beats"), list)
                and existing_visual_plan["beats"]
                and not request.replace_existing_visual_plan
            ):
                raise ValueError(
                    "refusing to replace the existing primer visual plan without explicit confirmation"
                )
            await self.prepare_visual_plan(
                episode,
                PrimerVisualPlanPrepareRequest(
                    user_id=request.user_id,
                    reuse_existing_script=bool(state.get("script")),
                    replace_existing_plan=request.replace_existing_visual_plan,
                ),
                narrator,
                model_endpoints,
            )
            if narrator.pronunciation.enabled:
                spoken_status = await self.prepare_spoken_script(
                    episode,
                    PrimerSpokenScriptPrepareRequest(user_id=request.user_id),
                    narrator,
                    model_endpoints,
                )
                if spoken_status.status != "approved":
                    return self.status(episode, narrator)
            await self.prepare_narration_timing(
                episode,
                PrimerNarrationTimingRequest(user_id=request.user_id),
                narrator,
                voice_profiles,
                voicebox_endpoints,
            )
            self.approve_visual_plan(
                episode,
                PrimerVisualPlanApprovalRequest(user_id=request.user_id or "automatic-draft"),
            )
        visual_plan = self._visual_plan_state(episode)
        visual_coverage = self._visual_plan_coverage(
            episode,
            visual_plan.get("beats") if isinstance(visual_plan.get("beats"), list) else [],
        )
        visual_plan["coverage"] = visual_coverage
        if visual_plan.get("status") != "approved" or not visual_coverage["ready"]:
            visual_plan["status"] = "review_required" if visual_plan.get("beats") else "blocked"
            visual_plan["failure"] = "; ".join(visual_coverage["blockers"])
            raise ValueError("prepare and approve the primer visual plan before production")
        existing_render = self._asset_by_id(episode, state.get("render_asset_id"))
        if (
            existing_render is not None
            and existing_render.status == "completed"
            and not request.regenerate
        ):
            return self.status(episode, narrator)

        target_duration_seconds = episode.definition.media.opening.target_duration_seconds
        existing_script = self._normalise_spoken_prose(str(state.get("script") or ""))
        if not existing_script:
            raise ValueError("the approved visual plan has no narration script")
        if not self._is_reusable_editorial_script(
            existing_script,
            state.get("editorial_polish"),
            self._target_word_count(target_duration_seconds),
        ):
            raise ValueError(
                "the primer narration script has not passed the spoken-prose quality gate"
            )
        if hashlib.sha256(existing_script.encode()).hexdigest() != visual_plan.get("script_checksum"):
            raise ValueError("primer narration changed; prepare and approve a new visual plan")
        if request.script_override:
            raise ValueError("use visual-plan preparation before supplying a new primer script")
        spoken_script = self._approved_spoken_script(
            episode,
            narrator,
            existing_script,
        )
        visual_assets = self._visual_plan_assets(episode, visual_plan)

        state.update(
            {
                "schema_version": "dialecticore.primer_production.v1",
                "status": "drafting",
                "narrator_profile_id": narrator.id,
                "narrator_profile_snapshot": narrator.model_dump(mode="json"),
                "target_duration_seconds": target_duration_seconds,
                "source_count": len(sources),
                "media_asset_count": len(visual_assets),
                "narration_quality": None,
                "failure": None,
                "started_at": datetime.now(UTC).isoformat(),
                "spoken_script_checksum": hashlib.sha256(
                    spoken_script.encode()
                ).hexdigest(),
            }
        )
        try:
            script = existing_script
            editorial_polish = state.get("editorial_polish") or {
                "status": "retained",
                "reason": "approved_visual_plan_script",
                "word_count": len(script.split()),
            }
            state["draft_script"] = script
            audio_asset = self._reusable_narration_asset(
                episode,
                state,
                spoken_script,
                narrator,
                request.reuse_existing_script and not request.regenerate_narration,
            )
            if audio_asset is not None:
                state["status"] = "reusing_approved_narration"
            else:
                state["status"] = "generating_narration"
                audio_asset = await self._narrate(
                    episode,
                    spoken_script,
                    narrator,
                    voice_profiles,
                    voicebox_endpoints,
                    editorial_script=script,
                )
                episode.assets.append(audio_asset)
                state["narration_asset_id"] = str(audio_asset.id)
            state["narration_quality"] = audio_asset.generation_metadata.get("transcription_qc")
            state["actual_narration_duration_ms"] = int(audio_asset.duration_ms or 0)
            timing = self._synchronise_visual_plan_to_narration(
                episode,
                audio_asset,
                script,
                actor=request.user_id or "web-ui",
            )
            state["narration_timing"] = timing
            if timing.get("review_required"):
                state.update(
                    {
                        "status": "visual_timing_review",
                        "failure": (
                            "the measured narrator track changed the visual timeline; "
                            "review and verify the revised source excerpts before rendering"
                        ),
                    }
                )
                episode.audit_events.append(
                    AuditEvent(
                        episode_id=episode.id,
                        event_type="primer.production.timing_review_required",
                        actor=request.user_id or "web-ui",
                        details={
                            "narrator_profile_id": narrator.id,
                            "narration_asset_id": str(audio_asset.id),
                            "duration_ms": audio_asset.duration_ms,
                            "regenerated_narration": request.regenerate_narration,
                        },
                    )
                )
                return self.status(episode, narrator)
            state["status"] = "building_timeline"
            timeline_asset = self._build_timeline(
                episode, audio_asset, visual_assets, narrator, visual_plan=visual_plan
            )
            episode.assets.append(timeline_asset)
            state["timeline_asset_id"] = str(timeline_asset.id)
            state["status"] = "rendering"
            render_asset = self._render(episode, timeline_asset, narrator)
            episode.assets.append(render_asset)
            state.update(
                {
                    "status": "completed",
                    "render_asset_id": str(render_asset.id),
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            )
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="primer.production.completed",
                    actor=request.user_id or "web-ui",
                    details={
                        "narrator_profile_id": narrator.id,
                        "narration_asset_id": str(audio_asset.id),
                        "timeline_asset_id": str(timeline_asset.id),
                        "render_asset_id": str(render_asset.id),
                        "duration_ms": render_asset.duration_ms,
                        "source_count": len(sources),
                        "media_asset_count": len(visual_assets),
                        "script_origin": editorial_polish.get("reason", "drafted"),
                        "editorial_polish": editorial_polish,
                    },
                )
            )
        except Exception as exc:
            state["status"] = "failed"
            state["failure"] = str(exc)
            state["failed_at"] = datetime.now(UTC).isoformat()
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="primer.production.failed",
                    actor=request.user_id or "web-ui",
                    details={"narrator_profile_id": narrator.id, "failure": str(exc)},
                )
            )
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"primer production failed: {exc}") from exc
        return self.status(episode, narrator)

    async def _draft_script(self, episode, narrator, endpoint, sources, target_seconds: int) -> str:
        target_words = self._target_word_count(target_seconds)
        evidence = "\n".join(
            f"- {item.get('title', 'Source')}: {item.get('summary', '')}" for item in sources[:6]
        )
        prompt = (
            f"Write {target_words} to {target_words + 20} German words for a {target_seconds}-second "
            f"voice-over primer for the topic: "
            f"{episode.definition.topic.central_question}. {narrator.editorial_style} "
            "Use only the supplied evidence. Structure it as hook, factual explanation, "
            "trade-off, and exactly one final question that leads into the panel. Do not phrase "
            "the central question as a separate question before the closing. Return plain spoken prose, "
            "without headings, citations, or invented facts.\nEvidence:\n" + evidence
        )
        if endpoint.base_url and endpoint.provider_type.value in {
            "openai_compatible",
            "generic_http",
        }:
            try:
                headers = auth_headers(endpoint, self.secret_resolver)
                payload = {
                    "model": narrator.model_id,
                    "messages": [
                        {"role": "system", "content": narrator.editorial_style},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": narrator.sampling_settings.temperature,
                    "top_p": narrator.sampling_settings.top_p,
                    "max_tokens": max(220, narrator.sampling_settings.max_tokens),
                    "reasoning": {"exclude": True},
                }
                async with httpx.AsyncClient(
                    base_url=endpoint.base_url.rstrip("/"),
                    timeout=endpoint.default_timeout_seconds,
                ) as client:
                    response = await client.post("/chat/completions", headers=headers, json=payload)
                    response.raise_for_status()
                content = response.json()["choices"][0]["message"].get("content")
                text = content.strip() if isinstance(content, str) else ""
                if len(text.split()) >= target_words:
                    return text
            except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError):
                pass
        return self._deterministic_script(episode, sources, target_seconds)

    async def _polish_script(
        self,
        episode: Episode,
        narrator: PrimerNarratorProfile,
        endpoint,
        sources: list[dict],
        target_seconds: int,
        draft: str,
    ) -> tuple[str, dict]:
        """Run the draft through the narrator model as a constrained spoken-prose edit."""
        target_words = self._target_word_count(target_seconds)
        evidence = "\n".join(
            f"- {item.get('title', 'Source')}: {item.get('summary', '')}"
            for item in sources[:6]
        )
        if not (
            endpoint.base_url
            and endpoint.provider_type.value in {"openai_compatible", "generic_http"}
        ):
            raise ValueError("the configured narration editor does not support text generation")
        prompt = (
            "Act as a senior German radio editor. Rewrite the draft below into fluent, natural "
            "spoken German for an evidence-led topic primer. Remove repetition, stock phrases, "
            "awkward transitions, and meta-commentary about the research process. Keep the meaning "
            "and factual limits of the supplied evidence. Do not introduce new facts, figures, "
            "attributions, or source claims. Use short, varied sentences and a clear progression: "
            "hook, explanation, trade-off, then exactly one final question that hands off to the panel. "
            "Do not ask the central question before the closing sentence. "
            f"Aim for roughly {target_seconds} seconds of spoken prose, usually about "
            f"{max(40, round(target_words * 0.75))} to {round(target_words * 1.35)} words. "
            "Prioritise a natural cadence over hitting an exact count. Return plain prose only, with no heading, "
            "bullets, citations, or prefatory note.\n\n"
            f"Central question: {episode.definition.topic.central_question}\n"
            f"Editorial brief: {episode.definition.media.opening.narration_brief or narrator.editorial_style}\n"
            f"Evidence:\n{evidence}\n\nDraft:\n{draft}"
        )
        try:
            polished = await self._request_editorial_rewrite(
                endpoint,
                narrator,
                prompt,
                temperature=min(0.45, narrator.sampling_settings.temperature),
            )
            issues = self._script_quality_issues(polished, target_words)
            if not issues:
                return polished, {
                    "status": "applied",
                    "model_endpoint_id": endpoint.id,
                    "model_id": narrator.model_id,
                    "word_count": len(polished.split()),
                }

            repair_prompt = (
                "Rewrite the candidate below as final German voice-over prose. It failed the "
                "production quality check for these reasons: "
                f"{'; '.join(issues)}. Return a complete replacement, not an explanation. "
                "Use only the supplied evidence. Use no heading, citation, bullet list, or source list. "
                "Write natural, varied sentences and finish with exactly one question that leads into the panel. "
                f"Keep the length flexible but appropriate for about {target_seconds} seconds.\n\n"
                f"Evidence:\n{evidence}\n\nCandidate:\n{polished}"
            )
            repaired = await self._request_editorial_rewrite(
                endpoint,
                narrator,
                repair_prompt,
                temperature=min(0.35, narrator.sampling_settings.temperature),
            )
            repair_issues = self._script_quality_issues(repaired, target_words)
            if not repair_issues:
                return repaired, {
                    "status": "applied_after_repair",
                    "model_endpoint_id": endpoint.id,
                    "model_id": narrator.model_id,
                    "word_count": len(repaired.split()),
                    "initial_validation_issues": issues,
                }
            raise ValueError(
                "the narration editor returned unusable spoken prose after a corrective retry: "
                f"{'; '.join(repair_issues)}"
            )
        except (httpx.HTTPError, AttributeError, KeyError, TypeError) as exc:
            raise ValueError(
                "the narration editor request failed; no fallback narration was created"
            ) from exc

    async def _request_editorial_rewrite(
        self,
        endpoint,
        narrator: PrimerNarratorProfile,
        prompt: str,
        *,
        temperature: float,
    ) -> str:
        headers = auth_headers(endpoint, self.secret_resolver)
        payload = {
            "model": narrator.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a rigorous German broadcast editor. Preserve factual limits and "
                        "return only final spoken prose."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "top_p": narrator.sampling_settings.top_p,
            "max_tokens": max(220, narrator.sampling_settings.max_tokens),
            "reasoning": {"exclude": True},
        }
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
        ) as client:
            response = await client.post("/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content")
        polished = self._normalise_spoken_prose(content if isinstance(content, str) else "")
        if not polished:
            raise ValueError("the narration editor returned no spoken prose")
        return polished

    @staticmethod
    def _normalise_spoken_prose(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().strip('"')

    @classmethod
    def _is_usable_polished_script(cls, script: str, target_words: int) -> bool:
        return not cls._script_quality_issues(script, target_words)

    @staticmethod
    def _script_quality_issues(script: str, target_words: int) -> list[str]:
        words = script.split()
        issues: list[str] = []
        if not (max(40, round(target_words * 0.60)) <= len(words) <= round(target_words * 1.50)):
            issues.append("word count is outside the flexible narration range")
        if not script.endswith("?"):
            issues.append("the final sentence is not the panel hand-off question")
        if script.count("?") != 1:
            issues.append("the script must contain exactly one question")
        sentences = [
            re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
            for sentence in re.split(r"(?<=[.!?])\s+", script)
            if sentence.strip()
        ]
        if len(sentences) < 4 or len(sentences) != len(set(sentences)):
            issues.append("sentences are too few or repeated")
        phrases = [" ".join(words[index : index + 8]).lower() for index in range(len(words) - 7)]
        if len(phrases) != len(set(phrases)):
            issues.append("an eight-word phrase is repeated")
        if re.search(r"https?://|(?:^|\s)[-*•]\s", script):
            issues.append("the script contains source-list or markup syntax")
        return issues

    @classmethod
    def _is_reusable_editorial_script(
        cls,
        script: str,
        editorial_polish: object,
        target_words: int,
    ) -> bool:
        if isinstance(editorial_polish, dict) and editorial_polish.get("status") in {
            "fallback",
            "blocked",
            "skipped",
        }:
            return False
        return cls._is_usable_polished_script(script, target_words)

    def _deterministic_script(
        self, episode: Episode, sources: list[dict], target_seconds: int
    ) -> str:
        target_words = self._target_word_count(target_seconds)
        source_titles = ", ".join(
            str(item.get("title") or "eine Quelle") for item in sources[:3]
        )
        question = episode.definition.topic.central_question
        dimensions = (
            ", ".join(episode.definition.topic.required_dimensions[:3])
            or "Folgen und Zielkonflikte"
        )
        body = [
            (
                "Dieses Thema beginnt nicht bei einer abstrakten Technologie, sondern bei den "
                "Entscheidungen, die ihre Infrastruktur moeglich machen. Im Mittelpunkt steht die Frage: "
                f"{question}"
            ),
            (
                f"Die Recherche stuetzt sich unter anderem auf {source_titles}. Sie ordnet das Thema "
                "aus unterschiedlichen Blickwinkeln ein und macht sichtbar, welche Fragen noch offen "
                "sind, welche Interessen aufeinandertreffen und welche Entscheidungen jetzt vorbereitet werden."
            ),
            (
                f"Fuer diese Einordnung schauen wir auf {dimensions}. Dabei geht es nicht nur um das, "
                "was technisch machbar ist. Ebenso wichtig sind Folgen fuer Menschen, Orte, Unternehmen "
                "und oeffentliche Aufgaben sowie die Frage, wie Nutzen, Kosten und Verantwortung verteilt werden."
            ),
            (
                "Ein guter Blick auf das Thema trennt belegte Informationen von Erwartungen und "
                "Versprechen. Er fragt nach Voraussetzungen, nach Zielkonflikten und danach, welche "
                "Folgen sich erst spaeter zeigen. Genau diese Perspektiven brauchen eine faire, konkrete Diskussion."
            ),
        ]
        words: list[str] = []
        closing = (
            "In der folgenden Runde treffen unterschiedliche Einschaetzungen aufeinander. Welche Entscheidungen "
            "sind heute noetig, damit die Entwicklung nachvollziehbar, verantwortbar und fuer die Gesellschaft "
            "tragfaehig bleibt?"
        ).split()
        body_budget = max(1, target_words - len(closing))
        for paragraph in body:
            paragraph_words = paragraph.split()
            if len(words) + len(paragraph_words) > body_budget:
                break
            words.extend(paragraph_words)
        for sentence in (
            "Sie legt offen, wo Fakten belastbar sind und wo politische oder wirtschaftliche Abwaegungen beginnen und begruendet werden.",
            "Die Quellen geben den Rahmen vor.",
            "Fehlende Informationen muessen dabei sichtbar bleiben.",
            "Das schafft klare Orientierung.",
        ):
            sentence_words = sentence.split()
            if len(words) + len(sentence_words) <= body_budget:
                words.extend(sentence_words)
        if not words:
            words.extend(
                "Die Recherche ordnet das Thema ein und zeigt, welche Fragen fuer die folgende Diskussion entscheidend sind."
                .split()[:body_budget]
            )
        return " ".join(words + closing)

    def _target_word_count(self, target_seconds: int) -> int:
        return max(40, round(target_seconds * self.settings.words_per_second))

    def _approved_editorial_script(self, episode: Episode) -> str:
        state = self._state(episode)
        script = self._normalise_spoken_prose(str(state.get("script") or ""))
        target_seconds = episode.definition.media.opening.target_duration_seconds
        if not self._is_reusable_editorial_script(
            script,
            state.get("editorial_polish"),
            self._target_word_count(target_seconds),
        ):
            raise ValueError("prepare an approved primer narration before pronunciation review")
        return script

    @staticmethod
    def _pronunciation_profile_fingerprint(narrator: PrimerNarratorProfile) -> str:
        payload = {
            "pronunciation": narrator.pronunciation.model_dump(mode="json"),
            "fallback_model_endpoint_id": narrator.model_endpoint_id,
            "fallback_model_id": narrator.model_id,
            "language": narrator.language,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def _spoken_token_signature(text: str) -> list[str]:
        return [
            token.casefold()
            for token in re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", text, flags=re.UNICODE)
        ]

    def _deterministic_pronunciation_replacements(
        self,
        script: str,
        narrator: PrimerNarratorProfile,
    ) -> list[PrimerSpokenScriptReplacement]:
        settings = narrator.pronunciation
        replacements: list[PrimerSpokenScriptReplacement] = []
        claimed_sources: set[str] = set()

        for entry in sorted(
            settings.custom_dictionary,
            key=lambda item: len(item.source),
            reverse=True,
        ):
            flags = 0 if entry.case_sensitive else re.IGNORECASE
            pattern = re.compile(
                rf"(?<!\w){re.escape(entry.source.strip())}(?!\w)",
                flags,
            )
            for match in pattern.finditer(script):
                source = match.group(0)
                if source in claimed_sources or source == entry.spoken.strip():
                    continue
                claimed_sources.add(source)
                replacements.append(
                    PrimerSpokenScriptReplacement(
                        source=source,
                        spoken=entry.spoken.strip(),
                        category=entry.category,
                        origin="dictionary",
                        reason="narrator pronunciation dictionary",
                    )
                )

        builtins = {
            "CO₂": ("Kohlenstoffdioxid", "unit"),
            "%": ("Prozent", "symbol"),
            "€": ("Euro", "symbol"),
            "&": ("und", "symbol"),
        }
        if settings.expand_units:
            builtins.update(
                {
                    "kWh": ("Kilowattstunden", "unit"),
                    "MWh": ("Megawattstunden", "unit"),
                    "GWh": ("Gigawattstunden", "unit"),
                    "TWh": ("Terawattstunden", "unit"),
                    "MW": ("Megawatt", "unit"),
                    "GW": ("Gigawatt", "unit"),
                }
            )
        for source, (spoken, category) in builtins.items():
            if source in script and source not in claimed_sources:
                claimed_sources.add(source)
                replacements.append(
                    PrimerSpokenScriptReplacement(
                        source=source,
                        spoken=spoken,
                        category=category,
                        origin="deterministic",
                        reason="built-in German broadcast pronunciation",
                    )
                )

        if settings.acronym_policy != "preserve":
            known_expansions = {
                "EU": "Europäische Union",
                "KI": "Künstliche Intelligenz",
            }
            for match in re.finditer(r"\b[A-ZÄÖÜ]{2,5}\b", script):
                source = match.group(0)
                if source in claimed_sources:
                    continue
                spoken = (
                    known_expansions.get(source)
                    if settings.acronym_policy == "expand_known"
                    else None
                ) or " ".join(source)
                if spoken == source:
                    continue
                claimed_sources.add(source)
                replacements.append(
                    PrimerSpokenScriptReplacement(
                        source=source,
                        spoken=spoken,
                        category="acronym",
                        origin="deterministic",
                        reason="configured acronym policy",
                    )
                )

        if settings.expand_numbers:
            number_pattern = re.compile(
                r"(?<!\w)(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d+)?(?!\w)"
            )
            language = narrator.language.split("-", 1)[0].lower() or "de"
            for match in number_pattern.finditer(script):
                source = match.group(0)
                start, end = match.span()
                if (
                    start >= 2
                    and script[start - 1] in "./"
                    and script[start - 2].isdigit()
                ) or (
                    end + 1 < len(script)
                    and script[end] in "./"
                    and script[end + 1].isdigit()
                ):
                    continue
                if source in claimed_sources:
                    continue
                integer_text, separator, decimal_text = source.partition(",")
                integer_value = int(integer_text.replace(".", "").replace(" ", ""))
                try:
                    spoken = str(num2words(integer_value, lang=language))
                    if separator:
                        decimal_words = " ".join(
                            str(num2words(int(digit), lang=language))
                            for digit in decimal_text
                        )
                        decimal_separator = "Komma" if language == "de" else "point"
                        spoken = f"{spoken} {decimal_separator} {decimal_words}"
                except (NotImplementedError, OverflowError, ValueError):
                    continue
                claimed_sources.add(source)
                replacements.append(
                    PrimerSpokenScriptReplacement(
                        source=source,
                        spoken=spoken,
                        category="number",
                        origin="deterministic",
                        reason="configured spoken-number expansion",
                    )
                )
        return replacements

    def _validated_pronunciation_replacements(
        self,
        script: str,
        replacements: list[PrimerSpokenScriptReplacement],
    ) -> list[PrimerSpokenScriptReplacement]:
        validated: list[PrimerSpokenScriptReplacement] = []
        sources: list[str] = []
        for replacement in replacements:
            source = replacement.source.strip()
            spoken = " ".join(replacement.spoken.split())
            if not source or not spoken:
                raise ValueError("pronunciation replacements require source and spoken text")
            if source not in script:
                raise ValueError(f"pronunciation source is not present in the editorial script: {source}")
            if re.search(r"https?://|[\r\n]", spoken):
                raise ValueError("spoken pronunciation replacements cannot contain URLs or line breaks")
            if source == spoken:
                continue
            if source in sources:
                existing = next(item for item in validated if item.source == source)
                if existing.spoken == spoken:
                    continue
                raise ValueError(f"conflicting pronunciation replacements for: {source}")
            sources.append(source)
            validated.append(
                replacement.model_copy(update={"source": source, "spoken": spoken})
            )
        return validated

    @staticmethod
    def _apply_pronunciation_replacements(
        script: str,
        replacements: list[PrimerSpokenScriptReplacement],
    ) -> str:
        if not replacements:
            return script
        by_source = {item.source: item.spoken for item in replacements}
        pattern = re.compile(
            "|".join(re.escape(source) for source in sorted(by_source, key=len, reverse=True))
        )
        return pattern.sub(lambda match: by_source[match.group(0)], script)

    async def _request_pronunciation_suggestions(
        self,
        endpoint,
        model_id: str,
        editorial_script: str,
        existing_replacements: list[PrimerSpokenScriptReplacement],
        narrator: PrimerNarratorProfile,
    ) -> dict:
        deterministic_script = self._apply_pronunciation_replacements(
            editorial_script,
            self._validated_pronunciation_replacements(
                editorial_script,
                existing_replacements,
            ),
        )
        prompt = (
            "Prepare German broadcast text for speech synthesis without rewriting its meaning. "
            "Return strict JSON with keys replacements and spoken_script. replacements is a list of "
            "objects with source, spoken, category, and reason. source must be an exact substring of "
            "the editorial script. Suggest only pronunciation changes for names, numbers, units, "
            "symbols, or acronyms not already transformed. spoken_script must equal the editorial "
            "script after the listed and existing replacements, except for punctuation and whitespace. "
            "Never add, remove, translate, summarize, reorder, or negate words. Preserve every number "
            "and factual qualifier. Use category acronym, name, number, unit, symbol, or custom.\n\n"
            f"Strictness: {narrator.pronunciation.strictness}\n"
            f"Expand numbers: {narrator.pronunciation.expand_numbers}\n"
            f"Expand units: {narrator.pronunciation.expand_units}\n"
            f"Optimize pauses: {narrator.pronunciation.optimize_pauses}\n\n"
            f"Editorial script:\n{editorial_script}\n\n"
            f"Existing replacements:\n"
            f"{json.dumps([item.model_dump(mode='json') for item in existing_replacements], ensure_ascii=False)}\n\n"
            f"Deterministic candidate:\n{deterministic_script}"
        )
        headers = auth_headers(endpoint, self.secret_resolver)
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a conservative German pronunciation editor. Return valid JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": min(0.2, narrator.sampling_settings.temperature),
            "top_p": min(0.9, narrator.sampling_settings.top_p),
            "max_tokens": max(800, narrator.sampling_settings.max_tokens),
            "response_format": {"type": "json_object"},
            **openrouter_reasoning_parameters(endpoint),
        }
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
        ) as client:
            response = await client.post("/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
        content = self._model_message_text(
            response.json()["choices"][0]["message"].get("content")
        )
        parsed = self._model_json_object(content)
        if not isinstance(parsed, dict):
            raise ValueError("pronunciation model did not return a JSON object")
        raw_replacements = parsed.get("replacements")
        if not isinstance(raw_replacements, list):
            raise ValueError("pronunciation model did not return replacements")
        existing_sources = {item.source for item in existing_replacements}
        suggestions: list[PrimerSpokenScriptReplacement] = []
        for raw in raw_replacements[:64]:
            if not isinstance(raw, dict):
                continue
            source = str(raw.get("source") or "").strip()
            spoken = str(raw.get("spoken") or "").strip()
            category = str(raw.get("category") or "custom")
            if source in existing_sources or category not in {
                "acronym",
                "name",
                "number",
                "unit",
                "symbol",
                "custom",
            }:
                continue
            suggestions.append(
                PrimerSpokenScriptReplacement(
                    source=source,
                    spoken=spoken,
                    category=category,
                    origin="ai",
                    reason=str(raw.get("reason") or "AI pronunciation suggestion")[:500],
                )
            )
        return {
            "replacements": suggestions,
            "spoken_script": str(parsed.get("spoken_script") or "").strip() or None,
        }

    def _approved_spoken_script(
        self,
        episode: Episode,
        narrator: PrimerNarratorProfile,
        editorial_script: str,
    ) -> str:
        if not narrator.pronunciation.enabled:
            return editorial_script
        status = self.spoken_script_status(episode, narrator)
        if status.status != "approved" or not status.spoken_script:
            raise ValueError(
                "prepare, review, and approve the pronunciation-ready spoken script "
                "before generating narration"
            )
        return status.spoken_script

    async def _narrate(
        self,
        episode,
        script,
        narrator,
        voice_profiles,
        endpoints,
        *,
        editorial_script: str | None = None,
    ) -> Asset:
        voice_profile = next(
            (item for item in voice_profiles if item.id == narrator.voice_profile_id), None
        )
        if voice_profile is None or not voice_profile.enabled:
            raise ValueError("narrator voice profile is not available")
        voice_profile = voice_profile.model_copy(update={"rate": narrator.delivery_rate})
        endpoint = next(
            (item for item in endpoints if item.id == voice_profile.voicebox_endpoint_id), None
        )
        if endpoint is None or not endpoint.enabled:
            raise ValueError("narrator Voicebox endpoint is not available")
        transcript = TranscriptVersion(
            episode_id=episode.id,
            type=TranscriptType.broadcast,
            language=narrator.language,
            status="approved",
        )
        turn = TranscriptTurn(
            transcript_version_id=transcript.id,
            source_discussion_turn_ids=[],
            speaker_participant_id="primer-narrator",
            text=script,
            status="approved",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=narrator.language,
            source_entity_type="primer_narration",
            source_entity_id=str(turn.id),
            mime_type="audio/wav",
            duration_ms=self.voicebox._estimate_duration_ms(script),
            generation_metadata={
                "primer": True,
                "narrator_profile_id": narrator.id,
                "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                "editorial_script_sha256": hashlib.sha256(
                    (editorial_script or script).encode()
                ).hexdigest(),
                "voice_profile_id": voice_profile.id,
                "voice_id": voice_profile.voice_id,
                "voicebox_endpoint_id": endpoint.id,
                "delivery_rate": voice_profile.rate,
            },
            status="submitted",
        )
        attempts = max(1, int(endpoint.capabilities.get("transcription_qc_max_attempts") or 2))
        result = None
        for attempt in range(1, attempts + 1):
            result = await self.voicebox._submit_tts(endpoint, voice_profile, transcript, turn, asset)
            result = await self.voicebox.verify_spoken_text(
                endpoint,
                result,
                expected_text=script,
                language=narrator.language,
            )
            transcription_qc = result.metadata.get("transcription_qc", {})
            if transcription_qc.get("status") == "passed":
                result = replace(
                    result,
                    metadata={
                        **result.metadata,
                        "transcription_qc": {
                            **transcription_qc,
                            "attempt": attempt,
                            "attempt_count": attempts,
                        },
                    },
                )
                break
            if transcription_qc.get("status") == "unavailable":
                raise ValueError(
                    "narration transcription QC is unavailable: "
                    f"{transcription_qc.get('reason', 'unknown reason')}"
                )
        if result is None or result.metadata.get("transcription_qc", {}).get("status") != "passed":
            reason_codes = result.metadata.get("transcription_qc", {}).get("reason_codes", []) if result else []
            raise ValueError(
                "narration transcription QC rejected generated speech"
                + (f": {', '.join(reason_codes)}" if reason_codes else "")
            )
        result = await self.voicebox._materialize_audio_result(
            endpoint,
            transcript,
            asset,
            result,
            voice_profile=voice_profile,
        )
        result = self.voicebox._with_timing_tracks(endpoint, turn, asset, result)
        if result.status != "completed" or not result.storage_uri:
            raise ValueError("narrator speech generation did not complete")
        asset.status = "completed"
        asset.storage_uri = result.storage_uri
        asset.mime_type = result.mime_type
        asset.duration_ms = result.duration_ms
        asset.checksum = result.checksum
        asset.generation_metadata = {**asset.generation_metadata, **result.metadata, "primer": True}
        return asset

    def _reusable_narration_asset(
        self,
        episode: Episode,
        state: dict,
        script: str,
        narrator: PrimerNarratorProfile,
        reuse_requested: bool,
    ) -> Asset | None:
        if not reuse_requested:
            return None
        asset = self._asset_by_id(episode, state.get("narration_asset_id"))
        if asset is None or asset.status != "completed" or asset.asset_type != AssetType.audio:
            return None
        metadata = asset.generation_metadata
        if metadata.get("narrator_profile_id") != narrator.id:
            return None
        delivery_pace = metadata.get("delivery_pace")
        generated_rate = (
            delivery_pace.get("rate")
            if isinstance(delivery_pace, dict)
            else metadata.get("delivery_rate")
        )
        try:
            if not math.isclose(float(generated_rate), narrator.delivery_rate, abs_tol=0.001):
                return None
        except (TypeError, ValueError):
            return None
        expected_script_sha256 = hashlib.sha256(script.encode()).hexdigest()
        qc = metadata.get("transcription_qc")
        qc_hash = qc.get("expected_text_sha256") if isinstance(qc, dict) else None
        if (
            metadata.get("script_sha256") != expected_script_sha256
            and qc_hash != expected_script_sha256
        ):
            return None
        if not isinstance(qc, dict) or qc.get("status") != "passed":
            return None
        return asset

    def _synchronise_visual_plan_to_narration(
        self,
        episode: Episode,
        narration: Asset,
        script: str,
        *,
        actor: str,
    ) -> dict:
        """Make a primer visual plan describe the actual, QC-passed WAV duration."""
        duration_ms = int(narration.duration_ms or 0)
        if duration_ms <= 0:
            raise ValueError("narrator audio has no measurable duration")
        visual_state = self._visual_plan_state(episode)
        beats = visual_state.get("beats")
        script_checksum = hashlib.sha256(script.encode()).hexdigest()
        configured_target_ms = episode.definition.media.opening.target_duration_seconds * 1000
        if not isinstance(beats, list) or not beats:
            return {
                "schema_version": "dialecticore.primer_narration_timing.v1",
                "status": "measured",
                "narration_asset_id": str(narration.id),
                "script_checksum": script_checksum,
                "duration_ms": duration_ms,
                "configured_target_duration_ms": configured_target_ms,
                "delta_ms": duration_ms - configured_target_ms,
                "review_required": False,
                "measured_at": datetime.now(UTC).isoformat(),
            }

        existing = visual_state.get("narration_timing")
        if self._narration_timing_matches(existing, narration, script_checksum):
            return existing

        previous_target_ms = self._visual_plan_target_duration_ms(episode, beats)
        requires_retiming = abs(previous_target_ms - duration_ms) > 500
        has_manual_excerpt_timing = any(
            isinstance(beat, dict)
            and beat.get("timing_source") == "verified_manual_excerpt"
            for beat in beats
        )
        if requires_retiming:
            self._archive_visual_plan(
                episode,
                reason="before synchronising visual timing to the measured narrator track",
                actor=actor,
            )
            original_durations = [
                max(1, self._optional_int(beat.get("duration_ms")) or 0)
                for beat in beats
                if isinstance(beat, dict)
            ]
            scaled_durations = self._rescale_durations(
                original_durations,
                target_duration_ms=duration_ms,
            )
            for beat, scaled_duration_ms in zip(beats, scaled_durations, strict=True):
                if not isinstance(beat, dict):
                    continue
                beat["duration_before_narration_timing_ms"] = beat.get("duration_ms")
                beat["duration_ms"] = scaled_duration_ms
                beat["target_narration_duration_ms"] = duration_ms
                if has_manual_excerpt_timing:
                    beat["narration_timing_review_required"] = True
            self._reflow_visual_plan_structure(episode, visual_state, beats)

        review_required = requires_retiming and has_manual_excerpt_timing
        timing = {
            "schema_version": "dialecticore.primer_narration_timing.v1",
            "status": "measured",
            "narration_asset_id": str(narration.id),
            "script_checksum": script_checksum,
            "duration_ms": duration_ms,
            "configured_target_duration_ms": configured_target_ms,
            "previous_visual_target_duration_ms": previous_target_ms,
            "delta_ms": duration_ms - configured_target_ms,
            "visual_timing_reflowed": requires_retiming,
            "review_required": review_required,
            "measured_at": datetime.now(UTC).isoformat(),
        }
        visual_state["narration_timing"] = timing
        coverage = self._visual_plan_coverage(episode, beats)
        visual_state["coverage"] = coverage
        if requires_retiming:
            visual_state.pop("approved_at", None)
            visual_state.pop("approved_by", None)
            visual_state["status"] = "review_required" if coverage["ready"] else "blocked"
            visual_state["failure"] = (
                "measured narration timing changed; review the reflowed source-video sequence"
                if coverage["ready"]
                else "; ".join(coverage["blockers"])
            )
            production_state = self._state(episode)
            prior_render = production_state.pop("render_asset_id", None)
            if prior_render:
                production_state["superseded_render_asset_id"] = prior_render
            production_state.pop("timeline_asset_id", None)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer.visual_plan.narration_timing.synchronised",
                actor=actor,
                details={
                    "narration_asset_id": str(narration.id),
                    "duration_ms": duration_ms,
                    "previous_visual_target_duration_ms": previous_target_ms,
                    "visual_timing_reflowed": requires_retiming,
                    "review_required": review_required,
                },
            )
        )
        return timing

    @staticmethod
    def _narration_timing_matches(
        timing: object,
        narration: Asset,
        script_checksum: str,
    ) -> bool:
        return (
            isinstance(timing, dict)
            and timing.get("status") == "measured"
            and timing.get("narration_asset_id") == str(narration.id)
            and PrimerProductionService._optional_int(timing.get("duration_ms"))
            == int(narration.duration_ms or 0)
            and timing.get("script_checksum") == script_checksum
        )

    @staticmethod
    def _rescale_durations(durations_ms: list[int], *, target_duration_ms: int) -> list[int]:
        if not durations_ms:
            return []
        target_duration_ms = max(len(durations_ms), target_duration_ms)
        source_total_ms = sum(max(1, duration_ms) for duration_ms in durations_ms)
        scaled = [
            max(1, round(max(1, duration_ms) * target_duration_ms / source_total_ms))
            for duration_ms in durations_ms
        ]
        difference = target_duration_ms - sum(scaled)
        for index in range(abs(difference)):
            target_index = len(scaled) - 1 - (index % len(scaled))
            if difference > 0:
                scaled[target_index] += 1
            elif scaled[target_index] > 1:
                scaled[target_index] -= 1
        return scaled

    def _build_timeline(
        self,
        episode,
        audio_asset,
        visual_assets,
        narrator,
        *,
        visual_plan: dict | None = None,
    ) -> Asset:
        duration_ms = int(audio_asset.duration_ms or 0)
        if duration_ms <= 0:
            raise ValueError("narrator audio has no measurable duration")
        planned_beats = (
            visual_plan.get("beats")
            if isinstance(visual_plan, dict) and isinstance(visual_plan.get("beats"), list)
            else []
        )
        if planned_beats:
            asset_by_id = {str(asset.id): asset for asset in visual_assets}
            planned_beats = [
                beat
                for beat in planned_beats
                if isinstance(beat, dict) and str(beat.get("asset_id") or "") in asset_by_id
            ]
        uses_reviewed_visual_plan = bool(planned_beats)
        if not planned_beats:
            planned_beats = [
                {
                    "asset_id": str(asset.id),
                    "source_start_ms": 0,
                    "source_end_ms": None,
                    "still_motion": "push_in",
                    "camera_transition": "source_reveal" if index == 0 else "dissolve",
                    "purpose": "source_media",
                }
                for index, asset in enumerate(visual_assets)
            ]
        if not planned_beats:
            raise ValueError("approved primer visual plan contains no usable source media")
        if uses_reviewed_visual_plan:
            scheduled_beats = self._sequence_timed_visual_beats(
                planned_beats,
                duration_ms=duration_ms,
            )
        else:
            planner = self._visual_planner_config(episode)
            planned_beats = self._paced_visual_beats(
                planned_beats,
                duration_ms=duration_ms,
                minimum_shot_duration_ms=planner["minimum_shot_duration_seconds"] * 1000,
            )
            scheduled_beats = [
                (beat, piece_duration, False)
                for beat, piece_duration in zip(
                    planned_beats,
                    self._split_duration(duration_ms, len(planned_beats)),
                    strict=True,
                )
            ]
        cursor = 0
        segments = []
        for index, (beat, piece_duration, terminal_clip) in enumerate(scheduled_beats, start=1):
            asset = next(
                (item for item in visual_assets if str(item.id) == str(beat.get("asset_id"))),
                None,
            )
            if asset is None:
                raise ValueError("approved primer visual plan references missing source media")
            segments.append(
                {
                    "id": f"primer-{index:02d}",
                    "start_ms": cursor,
                    "end_ms": cursor + piece_duration,
                    "duration_ms": piece_duration,
                    "audio_asset_id": str(audio_asset.id),
                    "audio_source_offset_ms": cursor,
                    "video_asset_id": str(asset.id),
                    "source_start_ms": int(beat.get("source_start_ms") or 0),
                    "source_end_ms": beat.get("source_end_ms"),
                    "still_motion": str(beat.get("still_motion") or "push_in"),
                    "source_range_timing_locked": (
                        beat.get("timing_source") == "verified_manual_excerpt"
                    ),
                    "terminal_clip": terminal_clip,
                    "terminal_fade_out_ms": (
                        self._terminal_fade_out_duration_ms(piece_duration)
                        if terminal_clip
                        else 0
                    ),
                    "visual_layers": [
                        {
                            "role": "video_primary",
                            "asset_id": str(asset.id),
                            "purpose": "source_media",
                        }
                    ],
                    "camera_transition": str(
                        beat.get("camera_transition")
                        or ("source_reveal" if index == 1 else "dissolve")
                    ),
                    "segment_type": "topic_primer",
                    "primer_beat": {
                        "id": beat.get("id"),
                        "purpose": beat.get("purpose"),
                        "narration_excerpt": beat.get("narration_excerpt"),
                        "planned_duration_ms": self._optional_int(beat.get("duration_ms")),
                        "timing_source": beat.get("timing_source") or "storyboard",
                        "terminal_clip": terminal_clip,
                    },
                    "citations": [
                        {
                            "title": beat.get("source_title")
                            or asset.generation_metadata.get("source_title")
                            or asset.generation_metadata.get("title")
                            or "Source",
                            "source_url": beat.get("source_url")
                            or asset.generation_metadata.get("source_url"),
                        }
                    ],
                }
            )
            cursor += piece_duration
        timeline = {
            "id": str(uuid4()),
            "schema_version": "dialecticore.primer_timeline.v2",
            "scope": "primer",
            "episode_id": str(episode.id),
            "language": narrator.language,
            "duration_ms": duration_ms,
            "visual_sequence": {
                "planned_duration_ms": sum(
                    max(0, self._optional_int(beat.get("duration_ms")) or 0)
                    for beat in planned_beats
                ),
                "rendered_duration_ms": duration_ms,
                "terminal_clip_applied": any(
                    terminal_clip for _beat, _duration, terminal_clip in scheduled_beats
                ),
            },
            "media": {"width": 1280, "height": 720, "fps": 24, "subtitle_mode": "burned_in"},
            "segments": segments,
            "tracks": {
                "video_primary": [item["id"] for item in segments],
                "audio_dialogue": [item["id"] for item in segments],
            },
            "chapters": [{"id": "primer", "start_ms": 0, "title": "Topic primer"}],
        }
        payload = json.dumps(timeline, indent=2, sort_keys=True).encode("utf-8")
        stored = self.object_store.put_bytes(
            key=f"timelines/{episode.id}/{timeline['id']}.primer.json",
            payload=payload,
            content_type="application/vnd.dialecticore.timeline+json",
        )
        return Asset(
            episode_id=episode.id,
            asset_type=AssetType.timeline,
            language=narrator.language,
            source_entity_type="primer_production",
            source_entity_id=timeline["id"],
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            duration_ms=duration_ms,
            checksum=stored.checksum,
            generation_metadata={
                "primer": True,
                "timeline_json": timeline,
                "object_storage_path": str(stored.path),
            },
            status="completed",
        )

    def _sequence_timed_visual_beats(
        self,
        beats: list[dict],
        *,
        duration_ms: int,
    ) -> list[tuple[dict, int, bool]]:
        """Keep approved visual timing intact and cut only the final displayed beat."""
        planned_duration_ms = sum(
            max(0, self._optional_int(beat.get("duration_ms")) or 0) for beat in beats
        )
        if planned_duration_ms < duration_ms:
            shortfall_ms = duration_ms - planned_duration_ms
            raise ValueError(
                "approved primer visual sequence is "
                f"{shortfall_ms / 1000:.1f}s shorter than the narration; "
                "extend a selected excerpt or add a visual beat"
            )
        remaining_ms = duration_ms
        scheduled: list[tuple[dict, int, bool]] = []
        for beat in beats:
            planned_beat_duration_ms = self._optional_int(beat.get("duration_ms")) or 0
            if planned_beat_duration_ms <= 0:
                raise ValueError("approved primer visual plan contains a beat without a duration")
            if remaining_ms <= 0:
                break
            rendered_duration_ms = min(planned_beat_duration_ms, remaining_ms)
            terminal_clip = rendered_duration_ms < planned_beat_duration_ms
            scheduled.append((beat, rendered_duration_ms, terminal_clip))
            remaining_ms -= rendered_duration_ms
        if remaining_ms > 0:
            raise ValueError("approved primer visual sequence contains no renderable duration")
        return scheduled

    @staticmethod
    def _terminal_fade_out_duration_ms(duration_ms: int) -> int:
        """Keep an automatic end fade brief enough not to obscure a short final excerpt."""
        return min(500, max(120, duration_ms // 4))

    def _apply_verified_manual_excerpt_timing(
        self,
        beats: list[dict],
        *,
        require_people_free_verification: bool,
    ) -> int:
        """Restore manually confirmed source ranges after a narrator-timing reflow.

        A measured narrator track may temporarily rescale every storyboard beat so
        the editor can review the revised schedule. The editor's verified video
        in/out points remain authoritative, however. Re-applying all verified
        manual excerpts here makes their real source duration drive the sequence
        again; only the final rendered excerpt may be shortened at render time.
        """
        applied_count = 0
        for beat in beats:
            if beat.get("timing_source") not in {
                "pending_manual_excerpt_verification",
                "verified_manual_excerpt",
            }:
                continue
            start_ms = self._optional_int(beat.get("source_start_ms"))
            end_ms = self._optional_int(beat.get("source_end_ms"))
            if start_ms is None or end_ms is None or end_ms <= start_ms:
                continue
            verification = beat.get("people_free_verification")
            if require_people_free_verification and not (
                isinstance(verification, dict)
                and verification.get("status") == "verified"
                and verification.get("people_visible") is False
            ):
                continue
            beat.update(
                {
                    "duration_ms": end_ms - start_ms,
                    "timing_source": "verified_manual_excerpt",
                    "manual_excerpt_duration_ms": end_ms - start_ms,
                    "source_range_strategy": "manual_verified_excerpt",
                }
            )
            applied_count += 1
        return applied_count

    @staticmethod
    def _reflow_visual_plan_beat_offsets(beats: list[dict]) -> None:
        cursor_ms = 0
        for beat in beats:
            duration_ms = max(
                0, PrimerProductionService._optional_int(beat.get("duration_ms")) or 0
            )
            beat["start_ms"] = cursor_ms
            beat["end_ms"] = cursor_ms + duration_ms
            cursor_ms += duration_ms

    def _reflow_visual_plan_structure(
        self,
        episode: Episode,
        state: dict,
        beats: list[dict],
    ) -> None:
        if not beats:
            return
        target_duration_ms = self._visual_plan_target_duration_ms(episode, beats)
        effective_durations = self._effective_visual_sequence_durations(
            beats,
            target_duration_ms,
        )
        excerpts = self._script_excerpts_for_durations(
            str(state.get("script") or ""),
            effective_durations,
        )
        for index, beat in enumerate(beats):
            if not isinstance(beat, dict):
                continue
            beat["target_narration_duration_ms"] = target_duration_ms
            beat["purpose"] = self._visual_beat_purpose(index, len(beats))
            beat["narration_excerpt"] = excerpts[index]
        self._reflow_visual_plan_beat_offsets(beats)

    def _refresh_visual_plan_review_state(
        self,
        episode: Episode,
        state: dict,
        beats: list[dict],
        *,
        event_type: str,
        actor: str,
        details: dict,
    ) -> dict:
        coverage = self._visual_plan_coverage(episode, beats)
        state.pop("approved_at", None)
        state.pop("approved_by", None)
        state.update(
            {
                "coverage": coverage,
                "status": "review_required" if coverage["ready"] else "blocked",
                "failure": None if coverage["ready"] else "; ".join(coverage["blockers"]),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type=event_type,
                actor=actor,
                details={**details, "beat_count": len(beats), "coverage": coverage},
            )
        )
        return coverage

    @staticmethod
    def _paced_visual_beats(
        beats: list[dict],
        *,
        duration_ms: int,
        minimum_shot_duration_ms: int,
    ) -> list[dict]:
        """Coalesce a reviewed beat plan when the spoken track is shorter than expected."""
        if len(beats) <= 1:
            return beats
        maximum_beats = max(1, duration_ms // max(1, minimum_shot_duration_ms))
        if len(beats) <= maximum_beats:
            return beats
        if maximum_beats == 1:
            return [beats[0]]
        indices = {
            round(index * (len(beats) - 1) / (maximum_beats - 1))
            for index in range(maximum_beats)
        }
        return [beat for index, beat in enumerate(beats) if index in indices]

    def _render(self, episode, timeline_asset, narrator) -> Asset:
        timeline = timeline_asset.generation_metadata["timeline_json"]
        preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
        render_id = str(uuid4())
        manifest = {"id": render_id, "scope": "primer", "timeline_asset_id": str(timeline_asset.id)}
        media = self.render._render_media_bytes(episode, timeline, preset, manifest, "primer")
        stored = self.object_store.put_bytes(
            key=f"renders/{episode.id}/{render_id}.primer.mp4",
            payload=media,
            content_type="video/mp4",
        )
        probe = self.render._probe_render(stored.path)
        return Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=narrator.language,
            source_entity_type="primer_timeline",
            source_entity_id=str(timeline_asset.id),
            storage_uri=stored.uri,
            mime_type="video/mp4",
            duration_ms=probe.get("duration_ms"),
            width=probe.get("width"),
            height=probe.get("height"),
            fps=probe.get("fps"),
            checksum=stored.checksum,
            generation_metadata={
                "primer": True,
                "render_scope": "primer",
                "timeline_asset_id": str(timeline_asset.id),
                "object_storage_path": str(stored.path),
                "media_probe": probe,
            },
            status="completed",
        )

    def _evidence_sources(self, episode: Episode) -> list[dict]:
        evidence = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
            ),
            None,
        )
        pack = evidence.generation_metadata.get("evidence_pack") if evidence else None
        return [
            item
            for item in (pack.get("source_index", []) if isinstance(pack, dict) else [])
            if isinstance(item, dict)
            and str(item.get("uri") or item.get("source_url") or "").startswith(
                ("https://", "http://")
            )
        ]

    def _build_visual_plan(
        self,
        *,
        episode: Episode,
        script: str,
        target_duration_seconds: int,
        visual_assets: list[Asset],
    ) -> dict:
        planner = self._visual_planner_config(episode)
        beat_count = max(
            3,
            math.ceil(target_duration_seconds / planner["target_shot_duration_seconds"]),
        )
        target_duration_ms = target_duration_seconds * 1000
        durations = self._split_duration(target_duration_ms, beat_count)
        excerpts = self._script_excerpts(script, beat_count)
        video_assets = [asset for asset in visual_assets if asset.asset_type == AssetType.video]
        image_assets = [asset for asset in visual_assets if asset.asset_type == AssetType.image]
        planned_assets = self._planned_visual_asset_sequence(
            video_assets=video_assets,
            image_assets=image_assets,
            beat_count=beat_count,
        )
        beats: list[dict] = []
        cursor = 0
        for index, duration_ms in enumerate(durations):
            asset = planned_assets[index] if index < len(planned_assets) else None
            motion = ("push_in", "pan_left", "pan_right", "pull_back")[index % 4]
            transition = "source_reveal" if index == 0 else "dissolve"
            beats.append(
                {
                    "id": f"primer-beat-{index + 1:02d}",
                    "start_ms": cursor,
                    "end_ms": cursor + duration_ms,
                    "duration_ms": duration_ms,
                    "target_narration_duration_ms": target_duration_ms,
                    "purpose": self._visual_beat_purpose(index, beat_count),
                    "narration_excerpt": excerpts[index],
                    "asset_id": str(asset.id) if asset else None,
                    "asset_type": asset.asset_type.value if asset else None,
                    "source_start_ms": 0,
                    "source_end_ms": None,
                    "source_title": (
                        asset.generation_metadata.get("source_title")
                        or asset.generation_metadata.get("title")
                        or "Source media"
                    )
                    if asset
                    else None,
                    "source_url": asset.generation_metadata.get("source_url") if asset else None,
                    "still_motion": motion,
                    "camera_transition": transition,
                    "visual_intent": self._visual_intent_for_beat(index, beat_count),
                    "selection_rationale": "duration-aware source schedule",
                    "selection_method": "duration_spread_v2",
                    "provenance": "source_media" if asset else "unassigned",
                    "review_status": "proposed",
                }
            )
            cursor += duration_ms
        self._assign_video_source_ranges(beats, visual_assets)
        coverage = self._visual_plan_coverage(episode, beats)
        return {
            "schema_version": "dialecticore.primer_storyboard.v2",
            "status": "review_required" if coverage["ready"] else "blocked",
            "script": script,
            "script_checksum": hashlib.sha256(script.encode()).hexdigest(),
            "target_duration_seconds": target_duration_seconds,
            "beats": beats,
            "coverage": coverage,
            "failure": None if coverage["ready"] else "; ".join(coverage["blockers"]),
            "prepared_at": datetime.now(UTC).isoformat(),
        }

    def _visual_planner_config(self, episode: Episode) -> dict:
        configured = episode.definition.media.opening.visual_planner
        settings = getattr(self, "settings", None)
        return {
            "enabled": configured.enabled,
            "model_endpoint_id": (
                configured.model_endpoint_id
                or getattr(settings, "primer_visual_planner_default_endpoint_id", None)
                or episode.definition.media.opening.media_discovery.model_endpoint_id
            ),
            "model_id": (
                configured.model_id
                or getattr(settings, "primer_visual_planner_default_model_id", None)
                or episode.definition.media.opening.media_discovery.model_id
            ),
            "automatic_draft_render": configured.automatic_draft_render,
            "target_shot_duration_seconds": max(
                configured.minimum_shot_duration_seconds,
                configured.target_shot_duration_seconds
                or getattr(settings, "primer_visual_planner_default_shot_duration_seconds", 6),
            ),
            "minimum_shot_duration_seconds": configured.minimum_shot_duration_seconds,
            "allow_generated_connective_visuals": configured.allow_generated_connective_visuals,
            "exclude_people": configured.exclude_people,
            "vision_required": configured.vision_required,
        }

    async def _assess_visual_suitability(
        self,
        *,
        visual_assets: list[Asset],
        planner: dict,
        model_endpoints: list,
    ) -> dict:
        """Verify whether source media satisfies the episode's people-free visual policy.

        The assessment is deliberately conservative: source assets that cannot be sampled and
        assessed are not eligible when the people-free policy is enabled. This is preferable to
        silently inserting presenter or interview footage into a narration-led primer.
        """
        if not planner["exclude_people"]:
            return {
                "status": "not_required",
                "reason": "people_allowed_by_episode_policy",
                "assessed_asset_count": 0,
                "eligible_asset_count": len(visual_assets),
            }

        pending_assets = [
            asset
            for asset in visual_assets
            if not self._asset_people_free_status(asset)[0]
            and not self._asset_people_detected(asset)
        ]
        if not pending_assets:
            eligible_count = len(self._people_free_visual_assets(visual_assets, planner))
            return {
                "status": "reused",
                "reason": "existing_visual_assessments",
                "assessed_asset_count": len(visual_assets),
                "eligible_asset_count": eligible_count,
            }

        endpoint = next(
            (item for item in model_endpoints if item.id == planner.get("model_endpoint_id")),
            None,
        )
        if endpoint is None or not endpoint.enabled or not planner.get("model_id"):
            self._mark_visual_assets_unverified(pending_assets, "vision_model_unavailable")
            return {
                "status": "unavailable",
                "reason": "vision_model_unavailable",
                "assessed_asset_count": 0,
                "eligible_asset_count": len(self._people_free_visual_assets(visual_assets, planner)),
            }
        if not (
            endpoint.base_url
            and endpoint.provider_type.value in {"openai_compatible", "generic_http"}
        ):
            self._mark_visual_assets_unverified(pending_assets, "vision_endpoint_not_supported")
            return {
                "status": "unavailable",
                "reason": "vision_endpoint_not_supported",
                "assessed_asset_count": 0,
                "eligible_asset_count": len(self._people_free_visual_assets(visual_assets, planner)),
            }

        frame_catalog = await asyncio.to_thread(
            self._visual_assessment_frame_catalog, pending_assets
        )
        if not frame_catalog:
            self._mark_visual_assets_unverified(pending_assets, "source_frames_unavailable")
            return {
                "status": "unavailable",
                "reason": "source_frames_unavailable",
                "assessed_asset_count": 0,
                "eligible_asset_count": len(self._people_free_visual_assets(visual_assets, planner)),
            }

        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "Assess every labelled source asset for a people-free current-affairs primer. "
                    "A visible person includes a face, human body, presenter, interview subject, crowd, "
                    "hand, silhouette, or person in the background. Return strict JSON only: "
                    "{\"assets\":[{\"asset_id\":\"...\",\"people_visible\":true|false,"
                    "\"summary\":\"short factual visual description\"}]}. Mark people_visible true "
                    "when uncertain. Do not infer content not visible in the supplied frames."
                ),
            }
        ]
        frame_counts: dict[str, int] = {}
        for item in frame_catalog:
            asset_id = item["asset_id"]
            frame_counts[asset_id] = frame_counts.get(asset_id, 0) + 1
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"Asset {asset_id}, title: {item['title']}, "
                        f"sample at {item['timestamp_ms'] / 1000:.1f} seconds."
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": item["data_url"]},
                }
            )
        payload = {
            "model": planner["model_id"],
            "messages": [
                {
                    "role": "system",
                    "content": "You are a conservative visual-content reviewer. Return valid JSON only.",
                },
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_tokens": max(
                1_600,
                len(pending_assets) * 400,
                int(endpoint.capabilities.get("minimum_structured_max_tokens") or 0),
            ),
            "response_format": {"type": "json_object"},
            **openrouter_reasoning_parameters(endpoint),
        }
        try:
            headers = auth_headers(endpoint, self.secret_resolver)
            async with httpx.AsyncClient(
                base_url=endpoint.base_url.rstrip("/"),
                timeout=endpoint.default_timeout_seconds,
            ) as client:
                response = await client.post("/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
            content_value = response.json()["choices"][0]["message"].get("content")
            assessed = self._parse_visual_suitability(
                self._model_message_text(content_value),
                pending_assets,
            )
        except (
            httpx.HTTPError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._mark_visual_assets_unverified(pending_assets, "vision_model_unavailable")
            return {
                "status": "unavailable",
                "reason": "vision_model_unavailable",
                "error_type": type(exc).__name__,
                "assessed_asset_count": 0,
                "eligible_asset_count": len(self._people_free_visual_assets(visual_assets, planner)),
            }

        if not assessed:
            self._mark_visual_assets_unverified(pending_assets, "vision_response_invalid")
            return {
                "status": "unavailable",
                "reason": "vision_response_invalid",
                "assessed_asset_count": 0,
                "eligible_asset_count": len(self._people_free_visual_assets(visual_assets, planner)),
            }

        now = datetime.now(UTC).isoformat()
        for asset in pending_assets:
            result = assessed.get(str(asset.id))
            if result is None:
                self._mark_visual_assets_unverified([asset], "vision_response_missing_asset")
                continue
            asset.generation_metadata = {
                **asset.generation_metadata,
                "primer_visual_suitability": {
                    "schema_version": "dialecticore.primer_visual_suitability.v1",
                    "status": "verified",
                    "people_visible": result["people_visible"],
                    "summary": result["summary"],
                    "sampled_frame_count": frame_counts.get(str(asset.id), 0),
                    "assessed_at": now,
                    "model_endpoint_id": endpoint.id,
                    "model_id": planner["model_id"],
                },
            }
        eligible_count = len(self._people_free_visual_assets(visual_assets, planner))
        return {
            "status": "assessed",
            "reason": "sampled_visual_review",
            "assessed_asset_count": len(assessed),
            "eligible_asset_count": eligible_count,
            "excluded_people_asset_count": sum(
                1 for asset in visual_assets if self._asset_people_detected(asset)
            ),
            "model_endpoint_id": endpoint.id,
            "model_id": planner["model_id"],
        }

    def _visual_assessment_frame_catalog(self, assets: list[Asset]) -> list[dict]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return []
        frames: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="dialecticore-primer-vision-") as temporary:
            directory = Path(temporary)
            for asset in assets:
                source_path = self.object_store.path_for_uri(asset.storage_uri or "")
                if source_path is None or not source_path.is_file():
                    continue
                timestamps = self._visual_assessment_timestamps(asset)
                for index, timestamp_ms in enumerate(timestamps):
                    output_path = directory / f"{asset.id}-{index}.jpg"
                    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
                    if asset.asset_type == AssetType.video:
                        command.extend(["-ss", f"{timestamp_ms / 1000:.3f}"])
                    command.extend(
                        [
                            "-i",
                            str(source_path),
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale=512:-2:force_original_aspect_ratio=decrease",
                            "-q:v",
                            "5",
                            str(output_path),
                        ]
                    )
                    try:
                        subprocess.run(
                            command,
                            check=True,
                            capture_output=True,
                            timeout=12,
                        )
                    except (subprocess.SubprocessError, OSError):
                        continue
                    if not output_path.is_file() or output_path.stat().st_size <= 0:
                        continue
                    payload = output_path.read_bytes()
                    frames.append(
                        {
                            "asset_id": str(asset.id),
                            "title": self._asset_source_title(asset),
                            "timestamp_ms": timestamp_ms,
                            "data_url": "data:image/jpeg;base64,"
                            + base64.b64encode(payload).decode("ascii"),
                        }
                    )
        return frames

    @staticmethod
    def _visual_assessment_timestamps(asset: Asset) -> list[int]:
        if asset.asset_type != AssetType.video:
            return [0]
        duration_ms = max(0, int(asset.duration_ms or 0))
        if duration_ms >= 12_000:
            return [round(duration_ms * fraction) for fraction in (0.15, 0.38, 0.62, 0.85)]
        return [1_000, 3_000, 5_000, 8_000]

    @staticmethod
    def _asset_source_title(asset: Asset) -> str:
        return str(
            asset.generation_metadata.get("source_title")
            or asset.generation_metadata.get("title")
            or "Source media"
        )[:512]

    @staticmethod
    def _parse_visual_suitability(content: str, assets: list[Asset]) -> dict[str, dict]:
        match = re.search(r"\{.*\}", content.strip(), flags=re.DOTALL)
        if match is None:
            return {}
        payload = json.loads(match.group(0))
        entries = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return {}
        allowed_ids = {str(asset.id) for asset in assets}
        result: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            asset_id = str(entry.get("asset_id") or entry.get("id") or "")
            people_visible = entry.get("people_visible")
            if not isinstance(people_visible, bool):
                people_visible = entry.get("contains_people", entry.get("has_people"))
            if asset_id not in allowed_ids or not isinstance(people_visible, bool):
                continue
            result[asset_id] = {
                "people_visible": people_visible,
                "summary": PrimerProductionService._normalise_storyboard_text(
                    entry.get("summary"), "sampled source media"
                ),
            }
        return result

    @staticmethod
    def _model_message_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _mark_visual_assets_unverified(assets: list[Asset], reason: str) -> None:
        for asset in assets:
            asset.generation_metadata = {
                **asset.generation_metadata,
                "primer_visual_suitability": {
                    "schema_version": "dialecticore.primer_visual_suitability.v1",
                    "status": "unverified",
                    "people_visible": None,
                    "reason": reason,
                },
            }

    @staticmethod
    def _asset_people_free_status(asset: Asset) -> tuple[bool, str]:
        assessment = asset.generation_metadata.get("primer_visual_suitability")
        if not isinstance(assessment, dict):
            return False, "not_assessed"
        if assessment.get("status") == "verified" and assessment.get("people_visible") is False:
            return True, "verified_no_people"
        return False, str(assessment.get("status") or "not_assessed")

    @staticmethod
    def _asset_people_detected(asset: Asset) -> bool:
        assessment = asset.generation_metadata.get("primer_visual_suitability")
        return isinstance(assessment, dict) and assessment.get("people_visible") is True

    def _beat_is_people_free(self, beat: dict, asset: Asset) -> bool:
        if asset.asset_type == AssetType.video:
            verification = beat.get("people_free_verification")
            return (
                isinstance(verification, dict)
                and verification.get("status") == "verified"
                and verification.get("people_visible") is False
            )
        return self._asset_people_free_status(asset)[0]

    def _people_free_visual_assets(self, visual_assets: list[Asset], planner: dict) -> list[Asset]:
        if not planner["exclude_people"]:
            return visual_assets
        return [
            asset
            for asset in visual_assets
            if asset.asset_type == AssetType.video or self._asset_people_free_status(asset)[0]
        ]

    async def _verify_people_free_video_windows(
        self,
        *,
        beats: list[dict],
        visual_assets: list[Asset],
        planner: dict,
        model_endpoints: list,
        candidate_windows: list[dict] | None = None,
        preserve_selected_ranges: bool = False,
    ) -> dict:
        """Choose people-free source-video windows for the actual storyboard beats.

        A video remains eligible even when it includes people elsewhere. The visual model reviews
        candidate excerpts, not the complete source, and the renderer only receives excerpts whose
        sampled beginning, middle, and end are all free of visible people.
        """
        video_assets = {
            str(asset.id): asset for asset in visual_assets if asset.asset_type == AssetType.video
        }
        video_beats: dict[str, list[dict]] = {}
        for beat in beats:
            asset_id = str(beat.get("asset_id") or "")
            if asset_id in video_assets:
                video_beats.setdefault(asset_id, []).append(beat)
        if not planner["exclude_people"]:
            return {
                "status": "not_required",
                "reason": "people_allowed_by_episode_policy",
                "verified_window_count": 0,
            }
        if not video_beats:
            return {
                "status": "unavailable",
                "reason": "no_source_video_beats",
                "verified_window_count": 0,
            }

        endpoint = next(
            (item for item in model_endpoints if item.id == planner.get("model_endpoint_id")),
            None,
        )
        if endpoint is None or not endpoint.enabled or not planner.get("model_id"):
            self._mark_video_beats_unverified(beats, "vision_model_unavailable")
            return {
                "status": "unavailable",
                "reason": "vision_model_unavailable",
                "verified_window_count": 0,
            }
        if not (
            endpoint.base_url
            and endpoint.provider_type.value in {"openai_compatible", "generic_http"}
        ):
            self._mark_video_beats_unverified(beats, "vision_endpoint_not_supported")
            return {
                "status": "unavailable",
                "reason": "vision_endpoint_not_supported",
                "verified_window_count": 0,
            }

        if candidate_windows is None:
            candidate_windows = self._candidate_video_windows(video_beats, video_assets)
        frame_catalog = await asyncio.to_thread(
            self._video_window_assessment_frame_catalog,
            candidate_windows,
            video_assets,
        )
        if not frame_catalog:
            self._mark_video_beats_unverified(beats, "source_window_frames_unavailable")
            return {
                "status": "unavailable",
                "reason": "source_window_frames_unavailable",
                "verified_window_count": 0,
            }

        frame_counts: dict[str, int] = {}
        for frame in frame_catalog:
            window_id = frame["window_id"]
            frame_counts[window_id] = frame_counts.get(window_id, 0) + 1
        frames_by_window: dict[str, list[dict]] = {}
        for frame in frame_catalog:
            frames_by_window.setdefault(frame["window_id"], []).append(frame)
        assessments: dict[str, dict] = {}
        review_failures = 0
        try:
            headers = auth_headers(endpoint, self.secret_resolver)
            async with httpx.AsyncClient(
                base_url=endpoint.base_url.rstrip("/"),
                timeout=endpoint.default_timeout_seconds,
            ) as client:
                # Four windows mean twelve reduced-resolution frames per request. This keeps the
                # model's structured decision bounded and retains results from healthy batches.
                for batch_start in range(0, len(candidate_windows), 4):
                    batch_windows = candidate_windows[batch_start : batch_start + 4]
                    batch_frames = [
                        frame
                        for window in batch_windows
                        for frame in frames_by_window.get(window["window_id"], [])
                    ]
                    if len(batch_frames) < len(batch_windows):
                        review_failures += len(batch_windows)
                        continue
                    payload = {
                        "model": planner["model_id"],
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a conservative visual-content reviewer. "
                                    "Return valid JSON only."
                                ),
                            },
                            {
                                "role": "user",
                                "content": self._video_window_review_content(batch_frames),
                            },
                        ],
                        "temperature": 0,
                        "max_tokens": max(
                            1_000,
                            len(batch_windows) * 120,
                            int(
                                endpoint.capabilities.get(
                                    "minimum_structured_max_tokens"
                                )
                                or 0
                            ),
                        ),
                        "response_format": {"type": "json_object"},
                        **openrouter_reasoning_parameters(endpoint),
                    }
                    try:
                        response = await client.post(
                            "/chat/completions", headers=headers, json=payload
                        )
                        response.raise_for_status()
                        content_value = response.json()["choices"][0]["message"].get("content")
                        batch_assessments = self._parse_video_window_suitability(
                            self._model_message_text(content_value), batch_windows
                        )
                    except (
                        httpx.HTTPError,
                        AttributeError,
                        KeyError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        review_failures += len(batch_windows)
                        continue
                    if not batch_assessments:
                        review_failures += len(batch_windows)
                        continue
                    assessments.update(batch_assessments)
                    review_failures += len(batch_windows) - len(batch_assessments)
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            self._mark_video_beats_unverified(beats, "vision_model_unavailable")
            return {
                "status": "unavailable",
                "reason": "vision_model_unavailable",
                "error_type": type(exc).__name__,
                "verified_window_count": 0,
            }
        if not assessments:
            self._mark_video_beats_unverified(beats, "vision_response_invalid")
            return {
                "status": "unavailable",
                "reason": "vision_response_invalid",
                "candidate_window_count": len(candidate_windows),
                "verified_window_count": 0,
            }

        safe_by_asset: dict[str, list[dict]] = {}
        for window in candidate_windows:
            assessment = assessments.get(window["window_id"])
            if assessment is not None and assessment["people_visible"] is False:
                safe_by_asset.setdefault(window["asset_id"], []).append(
                    {**window, **assessment}
                )
        now = datetime.now(UTC).isoformat()
        verified_window_count = 0
        blocked_beat_count = 0
        for asset_id, asset_beats in video_beats.items():
            safe_windows = sorted(
                safe_by_asset.get(asset_id, []), key=lambda window: window["start_ms"]
            )
            asset = video_assets[asset_id]
            asset.generation_metadata = {
                **asset.generation_metadata,
                "primer_visual_window_assessment": {
                    "schema_version": "dialecticore.primer_visual_window_assessment.v1",
                    "status": "assessed",
                    "candidate_window_count": sum(
                        1 for window in candidate_windows if window["asset_id"] == asset_id
                    ),
                    "reviewed_window_count": sum(
                        1
                        for window in candidate_windows
                        if window["asset_id"] == asset_id
                        and window["window_id"] in assessments
                    ),
                    "safe_window_count": len(safe_windows),
                    "assessed_at": now,
                    "model_endpoint_id": endpoint.id,
                    "model_id": planner["model_id"],
                },
            }
            if preserve_selected_ranges:
                windows_by_beat_id = {
                    str(window.get("beat_id") or ""): window
                    for window in candidate_windows
                    if window.get("asset_id") == asset_id
                }
                for beat in asset_beats:
                    window = windows_by_beat_id.get(str(beat.get("id") or ""))
                    assessment = assessments.get(str(window.get("window_id") or "")) if window else None
                    if assessment is None or assessment["people_visible"] is not False:
                        blocked_beat_count += 1
                        beat["people_free_verification"] = {
                            "status": "not_verified",
                            "people_visible": (
                                assessment["people_visible"] if assessment is not None else None
                            ),
                            "reason": (
                                "people_detected_in_manual_excerpt"
                                if assessment is not None
                                else "manual_excerpt_review_incomplete"
                            ),
                        }
                        continue
                    beat["people_free_verification"] = {
                        "status": "verified",
                        "people_visible": False,
                        "window_id": window["window_id"],
                        "summary": assessment["summary"],
                        "sampled_frame_count": frame_counts.get(window["window_id"], 0),
                        "selection_method": "manual_trim",
                        "assessed_at": now,
                        "model_endpoint_id": endpoint.id,
                        "model_id": planner["model_id"],
                    }
                    beat["source_range_strategy"] = "manual_verified_excerpt"
                    verified_window_count += 1
                continue
            if len(safe_windows) < len(asset_beats):
                blocked_beat_count += len(asset_beats)
                self._mark_video_beats_unverified(
                    asset_beats,
                    "insufficient_people_free_windows",
                )
                continue
            for index, beat in enumerate(asset_beats):
                window = safe_windows[(index * len(safe_windows)) // len(asset_beats)]
                beat.update(
                    {
                        "source_start_ms": window["start_ms"],
                        "source_end_ms": window["end_ms"],
                        "source_range_strategy": (
                            "people_free_scene_excerpt"
                            if window.get("selection_method") == "scene_detected"
                            else "people_free_window_scan"
                        ),
                        "people_free_verification": {
                            "status": "verified",
                            "people_visible": False,
                            "window_id": window["window_id"],
                            "summary": window["summary"],
                            "sampled_frame_count": frame_counts.get(window["window_id"], 0),
                            "selection_method": window.get("selection_method"),
                            "assessed_at": now,
                            "model_endpoint_id": endpoint.id,
                            "model_id": planner["model_id"],
                        },
                    }
                )
                verified_window_count += 1
        return {
            "status": "assessed",
            "reason": (
                "manual_excerpt_review"
                if preserve_selected_ranges
                else "scene_aware_candidate_excerpt_scan"
            ),
            "candidate_window_count": len(candidate_windows),
            "scene_window_count": sum(
                1
                for window in candidate_windows
                if window.get("selection_method") == "scene_detected"
            ),
            "fallback_window_count": sum(
                1
                for window in candidate_windows
                if window.get("selection_method") == "duration_spread_fallback"
            ),
            "verified_window_count": verified_window_count,
            "reviewed_window_count": len(assessments),
            "unreviewed_window_count": review_failures,
            "blocked_beat_count": blocked_beat_count,
            "model_endpoint_id": endpoint.id,
            "model_id": planner["model_id"],
        }

    def _candidate_video_windows(
        self,
        video_beats: dict[str, list[dict]],
        video_assets: dict[str, Asset],
    ) -> list[dict]:
        windows: list[dict] = []
        for asset_id, asset_beats in video_beats.items():
            # Prefer editorially coherent shots. A uniform scan is retained only to cover
            # long sources with few detectable cuts, not as the primary source-selection rule.
            candidate_count = min(12, max(8, len(asset_beats) * 3))
            asset = video_assets[asset_id]
            required_duration_ms = max(
                int(beat.get("duration_ms") or 0) for beat in asset_beats
            )
            scene_windows = self._scene_detected_video_windows(
                asset,
                required_duration_ms=required_duration_ms,
                maximum_count=candidate_count,
            )
            ranges = {
                (int(window["start_ms"]), int(window["end_ms"])): window
                for window in scene_windows
            }
            fallback_count = max(0, candidate_count - len(ranges))
            for index in range(fallback_count):
                start_ms, end_ms, _analysis = self._planned_video_source_range(
                    asset,
                    required_duration_ms,
                    occurrence_index=index,
                    occurrence_count=max(1, fallback_count),
                )
                ranges.setdefault(
                    (start_ms, end_ms),
                    {
                        "asset_id": asset_id,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "selection_method": "duration_spread_fallback",
                    },
                )
            for index, window in enumerate(
                sorted(ranges.values(), key=lambda item: (item["start_ms"], item["end_ms"])),
                start=1,
            ):
                windows.append(
                    {
                        **window,
                        "window_id": f"window-{asset_id[:8]}-{index:02d}",
                    }
                )
        return windows

    def _scene_detected_video_windows(
        self,
        asset: Asset,
        *,
        required_duration_ms: int,
        maximum_count: int,
    ) -> list[dict]:
        """Return bounded short excerpts centred within detected source-video scenes.

        Scene detection only proposes visual units. Every returned excerpt is subsequently
        checked by the configured vision model, so a cut boundary never overrides the
        episode's people-free policy.
        """
        ffmpeg = shutil.which("ffmpeg")
        source_path = self.object_store.path_for_uri(asset.storage_uri or "")
        source_duration_ms = max(0, int(asset.duration_ms or 0))
        if (
            ffmpeg is None
            or source_path is None
            or not source_path.is_file()
            or source_duration_ms <= 0
        ):
            return []
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostats",
                    "-loglevel",
                    "info",
                    "-i",
                    str(source_path),
                    "-an",
                    "-vf",
                    "scale=320:-2,select='gt(scene,0.30)',showinfo",
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=min(150, max(30, math.ceil(source_duration_ms / 4_000))),
            )
        except (subprocess.SubprocessError, OSError):
            return []
        cut_points = [
            round(float(value) * 1000)
            for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr)
        ]
        return self._scene_video_windows_from_cut_points(
            asset_id=str(asset.id),
            source_duration_ms=source_duration_ms,
            required_duration_ms=required_duration_ms,
            maximum_count=maximum_count,
            cut_points_ms=cut_points,
        )

    @staticmethod
    def _scene_video_windows_from_cut_points(
        *,
        asset_id: str,
        source_duration_ms: int,
        required_duration_ms: int,
        maximum_count: int,
        cut_points_ms: list[int],
    ) -> list[dict]:
        """Build 4-15 second scene-contained windows from ffmpeg cut timestamps."""
        required_duration_ms = max(4_000, min(15_000, required_duration_ms))
        if source_duration_ms < required_duration_ms or maximum_count <= 0:
            return []
        bumper_skip_ms = min(5_000, max(1_500, source_duration_ms // 20))
        tail_skip_ms = min(3_000, max(1_000, source_duration_ms // 30))
        usable_start_ms = bumper_skip_ms
        usable_end_ms = max(usable_start_ms + required_duration_ms, source_duration_ms - tail_skip_ms)
        boundary_points = [usable_start_ms]
        for timestamp_ms in sorted(set(cut_points_ms)):
            if usable_start_ms < timestamp_ms < usable_end_ms:
                boundary_points.append(timestamp_ms)
        boundary_points.append(usable_end_ms)

        candidates: list[dict] = []
        excerpt_duration_ms = min(15_000, required_duration_ms + 2_000)
        for start_ms, end_ms in zip(boundary_points, boundary_points[1:], strict=False):
            scene_duration_ms = end_ms - start_ms
            if scene_duration_ms < required_duration_ms:
                continue
            window_duration_ms = min(scene_duration_ms, excerpt_duration_ms)
            window_start_ms = start_ms + (scene_duration_ms - window_duration_ms) // 2
            candidates.append(
                {
                    "asset_id": asset_id,
                    "start_ms": window_start_ms,
                    "end_ms": window_start_ms + window_duration_ms,
                    "selection_method": "scene_detected",
                    "scene_start_ms": start_ms,
                    "scene_end_ms": end_ms,
                }
            )
        if len(candidates) <= maximum_count:
            return candidates
        if maximum_count == 1:
            return [candidates[len(candidates) // 2]]
        selected: list[dict] = []
        for index in range(maximum_count):
            candidate_index = round(index * (len(candidates) - 1) / (maximum_count - 1))
            candidate = candidates[candidate_index]
            if candidate not in selected:
                selected.append(candidate)
        return selected

    def _video_window_assessment_frame_catalog(
        self,
        windows: list[dict],
        video_assets: dict[str, Asset],
    ) -> list[dict]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return []
        frames: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="dialecticore-primer-window-vision-") as temporary:
            directory = Path(temporary)
            for window in windows:
                asset = video_assets[window["asset_id"]]
                source_path = self.object_store.path_for_uri(asset.storage_uri or "")
                if source_path is None or not source_path.is_file():
                    continue
                start_ms = int(window["start_ms"])
                end_ms = int(window["end_ms"])
                for index, fraction in enumerate((0.2, 0.5, 0.8)):
                    timestamp_ms = round(start_ms + (end_ms - start_ms) * fraction)
                    output_path = directory / f"{window['window_id']}-{index}.jpg"
                    command = [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        f"{timestamp_ms / 1000:.3f}",
                        "-i",
                        str(source_path),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=512:-2:force_original_aspect_ratio=decrease",
                        "-q:v",
                        "5",
                        str(output_path),
                    ]
                    try:
                        subprocess.run(
                            command,
                            check=True,
                            capture_output=True,
                            timeout=12,
                        )
                    except (subprocess.SubprocessError, OSError):
                        continue
                    if not output_path.is_file() or output_path.stat().st_size <= 0:
                        continue
                    frames.append(
                        {
                            "window_id": window["window_id"],
                            "asset_id": window["asset_id"],
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "timestamp_ms": timestamp_ms,
                            "data_url": "data:image/jpeg;base64,"
                            + base64.b64encode(output_path.read_bytes()).decode("ascii"),
                        }
                    )
        return frames

    @staticmethod
    def _video_window_review_content(frames: list[dict]) -> list[dict]:
        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "Review each labelled short video window for a people-free editorial primer. "
                    "A visible person includes a face, human body, presenter, interview subject, crowd, "
                    "hand, silhouette, or person in the background. A window is compliant only if every "
                    "sample frame is free of people. Return strict JSON only: "
                    "{\"windows\":[{\"window_id\":\"...\",\"people_visible\":true|false}]}. "
                    "Include every labelled window exactly once. Mark people_visible true when uncertain. "
                    "Do not infer content not visible in the supplied frames."
                ),
            }
        ]
        for frame in frames:
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"Window {frame['window_id']}, source {frame['asset_id']}, "
                        f"excerpt {frame['start_ms'] / 1000:.1f}-{frame['end_ms'] / 1000:.1f}s, "
                        f"sample at {frame['timestamp_ms'] / 1000:.1f}s."
                    ),
                }
            )
            content.append({"type": "image_url", "image_url": {"url": frame["data_url"]}})
        return content

    @staticmethod
    def _parse_video_window_suitability(content: str, windows: list[dict]) -> dict[str, dict]:
        payload = PrimerProductionService._model_json_object(content)
        if payload is None:
            return {}
        entries = payload.get("windows") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return {}
        allowed_ids = {window["window_id"] for window in windows}
        result: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            window_id = str(entry.get("window_id") or entry.get("id") or "")
            people_visible = entry.get("people_visible")
            if not isinstance(people_visible, bool):
                people_visible = entry.get("contains_people", entry.get("has_people"))
            if window_id not in allowed_ids or not isinstance(people_visible, bool):
                continue
            result[window_id] = {
                "people_visible": people_visible,
                "summary": PrimerProductionService._normalise_storyboard_text(
                    entry.get("summary"), "sampled source-video excerpt"
                ),
            }
        return result

    @staticmethod
    def _model_json_object(content: str) -> dict | None:
        """Extract the first complete JSON object from a model response safely."""
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", content):
            try:
                value, _end = decoder.raw_decode(content[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _mark_video_beats_unverified(beats: list[dict], reason: str) -> None:
        for beat in beats:
            beat["people_free_verification"] = {
                "status": "not_verified",
                "people_visible": None,
                "reason": reason,
            }

    async def _apply_ai_storyboard_plan(
        self,
        *,
        episode: Episode,
        script: str,
        visual_assets: list[Asset],
        beats: list[dict],
        planner: dict,
        model_endpoints: list,
    ) -> dict:
        """Use a configured model for semantic, people-free source assignment."""
        result = {
            "status": "fallback",
            "reason": "planner_not_configured",
            "model_endpoint_id": planner.get("model_endpoint_id"),
            "model_id": planner.get("model_id"),
            "automatic_draft_render": planner["automatic_draft_render"],
        }
        if not planner["enabled"]:
            result["reason"] = "planner_disabled"
            return result
        endpoint = next(
            (item for item in model_endpoints if item.id == planner.get("model_endpoint_id")),
            None,
        )
        if endpoint is None or not endpoint.enabled or not planner.get("model_id"):
            result["reason"] = "planner_endpoint_unavailable"
            return result
        if planner["vision_required"] and endpoint.capabilities.get("vision") is not True:
            result["reason"] = "configured_planner_lacks_required_vision_capability"
            return result
        if not (
            endpoint.base_url
            and endpoint.provider_type.value in {"openai_compatible", "generic_http"}
        ):
            result["reason"] = "planner_endpoint_not_openai_compatible"
            return result

        catalog = [
            {
                "asset_id": str(asset.id),
                "type": asset.asset_type.value,
                "title": (
                    asset.generation_metadata.get("source_title")
                    or asset.generation_metadata.get("title")
                    or "Source media"
                ),
                "duration_seconds": round((asset.duration_ms or 0) / 1000, 1)
                if asset.asset_type == AssetType.video
                else None,
                "source_url": asset.generation_metadata.get("source_url"),
            }
            for asset in visual_assets
        ]
        beat_brief = [
            {
                "id": beat["id"],
                "purpose": beat["purpose"],
                "narration_excerpt": beat["narration_excerpt"],
                "duration_seconds": round(int(beat["duration_ms"]) / 1000, 1),
            }
            for beat in beats
        ]
        prompt = (
            "You are the visual editor for a concise German current-affairs primer. Match each "
            "narration beat to the most relevant people-free source asset from the supplied catalog. "
            "Build a calm, factual sequence with readable visual holds. Do not manufacture variety or "
            "change the requested number of beats; reuse a source when it best supports the narration. "
            "Do not invent visual claims, never introduce people, and use a smooth dissolve after the "
            "opening reveal. Return "
            "strict JSON only in this form: {\"beats\":[{\"id\":\"primer-beat-01\",\"asset_id\":"
            "\"catalog id\",\"visual_intent\":\"short phrase\",\"selection_rationale\":\"short "
            "evidence-bound reason\",\"camera_transition\":\"dissolve|source_reveal\"}]}. "
            "Every requested beat must appear exactly once and asset_id must be from the catalog.\n\n"
            f"Topic: {episode.definition.topic.central_question}\n"
            f"Narration: {script}\n\nBeats:\n{json.dumps(beat_brief, ensure_ascii=False)}\n\n"
            f"Source catalog:\n{json.dumps(catalog, ensure_ascii=False)}"
        )
        try:
            headers = auth_headers(endpoint, self.secret_resolver)
            payload = {
                "model": planner["model_id"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                    "You are an evidence-led video editor. Return compact valid JSON and no prose."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.25,
                "top_p": 0.9,
                "max_tokens": max(
                    1_600,
                    len(beats) * 180,
                    int(endpoint.capabilities.get("minimum_structured_max_tokens") or 0),
                ),
                "response_format": {"type": "json_object"},
                **openrouter_reasoning_parameters(endpoint),
            }
            async with httpx.AsyncClient(
                base_url=endpoint.base_url.rstrip("/"),
                timeout=endpoint.default_timeout_seconds,
            ) as client:
                response = await client.post("/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content")
            selected = self._parse_storyboard_selection(
                self._model_message_text(content),
                beats,
                visual_assets,
            )
            if not selected:
                result["reason"] = "planner_output_failed_validation"
                return result
            for beat in beats:
                suggestion = selected.get(str(beat["id"]))
                if suggestion is None:
                    continue
                asset = next(
                    (item for item in visual_assets if str(item.id) == suggestion["asset_id"]),
                    None,
                )
                if asset is None:
                    continue
                beat.update(
                    {
                        "asset_id": str(asset.id),
                        "asset_type": asset.asset_type.value,
                        "source_title": (
                            asset.generation_metadata.get("source_title")
                            or asset.generation_metadata.get("title")
                            or "Source media"
                        ),
                        "source_url": asset.generation_metadata.get("source_url"),
                        "visual_intent": suggestion["visual_intent"],
                        "selection_rationale": suggestion["selection_rationale"],
                        "camera_transition": suggestion["camera_transition"],
                        "selection_method": "ai_storyboard_v2",
                        "provenance": "source_media",
                    }
                )
            result.update({"status": "applied", "reason": "structured_source_assignment"})
            return result
        except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            result["reason"] = "planner_model_unavailable"
            result["error_type"] = type(exc).__name__
            return result

    def _parse_storyboard_selection(
        self,
        content: str,
        beats: list[dict],
        visual_assets: list[Asset],
    ) -> dict[str, dict] | None:
        if not content.strip():
            return None
        match = re.search(r"\{.*\}", content.strip(), flags=re.DOTALL)
        if match is None:
            return None
        payload = json.loads(match.group(0))
        entries = payload.get("beats") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return None
        valid_beat_ids = {str(beat["id"]) for beat in beats}
        valid_asset_ids = {str(asset.id) for asset in visual_assets}
        selected: dict[str, dict] = {}
        previous_asset_id: str | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            beat_id = str(entry.get("id") or "")
            asset_id = str(entry.get("asset_id") or "")
            if (
                beat_id not in valid_beat_ids
                or asset_id not in valid_asset_ids
                or beat_id in selected
                or asset_id == previous_asset_id
            ):
                return None
            transition = str(entry.get("camera_transition") or "dissolve")
            if transition not in {"dissolve", "source_reveal"}:
                transition = "dissolve"
            selected[beat_id] = {
                "asset_id": asset_id,
                "visual_intent": self._normalise_storyboard_text(
                    entry.get("visual_intent"), "source context"
                ),
                "selection_rationale": self._normalise_storyboard_text(
                    entry.get("selection_rationale"), "source matches narration beat"
                ),
                "camera_transition": transition,
            }
            previous_asset_id = asset_id
        return selected if set(selected) == valid_beat_ids else None

    @staticmethod
    def _normalise_storyboard_text(value: object, fallback: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:360] if text else fallback

    @staticmethod
    def _planned_visual_asset_sequence(
        *, video_assets: list[Asset], image_assets: list[Asset], beat_count: int
    ) -> list[Asset | None]:
        if not video_assets and not image_assets:
            return [None] * beat_count
        image_positions = (
            set(range(2, beat_count, 5)) if video_assets and image_assets else set()
        )
        sequence: list[Asset | None] = []
        video_index = 0
        image_index = 0
        for index in range(beat_count):
            if index in image_positions:
                sequence.append(image_assets[image_index % len(image_assets)])
                image_index += 1
            elif video_assets:
                sequence.append(video_assets[video_index % len(video_assets)])
                video_index += 1
            else:
                sequence.append(image_assets[image_index % len(image_assets)])
                image_index += 1
        return sequence

    def _assign_video_source_ranges(self, beats: list[dict], visual_assets: list[Asset]) -> None:
        assets = {str(asset.id): asset for asset in visual_assets}
        occurrences: dict[str, list[dict]] = {}
        for beat in beats:
            asset = assets.get(str(beat.get("asset_id") or ""))
            if asset is not None and asset.asset_type == AssetType.video:
                occurrences.setdefault(str(asset.id), []).append(beat)
        for asset_id, asset_beats in occurrences.items():
            asset = assets[asset_id]
            for index, beat in enumerate(asset_beats):
                start_ms, end_ms, analysis = self._planned_video_source_range(
                    asset,
                    int(beat.get("duration_ms") or 0),
                    occurrence_index=index,
                    occurrence_count=len(asset_beats),
                )
                beat["source_start_ms"] = start_ms
                beat["source_end_ms"] = end_ms
                beat["source_range_strategy"] = analysis["strategy"]
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "primer_visual_analysis": analysis,
                }

    @staticmethod
    def _planned_video_source_range(
        asset: Asset,
        beat_duration_ms: int,
        *,
        occurrence_index: int,
        occurrence_count: int,
    ) -> tuple[int, int, dict]:
        source_duration_ms = max(0, int(asset.duration_ms or 0))
        required_duration_ms = max(2_500, beat_duration_ms + 700)
        if source_duration_ms <= required_duration_ms:
            return (
                0,
                required_duration_ms,
                {
                    "schema_version": "dialecticore.primer_source_analysis.v1",
                    "strategy": "duration_unknown_or_short",
                    "source_duration_ms": source_duration_ms or None,
                    "usable_start_ms": 0,
                    "usable_end_ms": source_duration_ms or required_duration_ms,
                },
            )
        bumper_skip_ms = min(5_000, max(1_500, source_duration_ms // 20))
        tail_skip_ms = min(3_000, max(1_000, source_duration_ms // 30))
        usable_start_ms = bumper_skip_ms
        usable_end_ms = max(usable_start_ms + required_duration_ms, source_duration_ms - tail_skip_ms)
        latest_start_ms = max(usable_start_ms, usable_end_ms - required_duration_ms)
        fraction = (occurrence_index + 1) / (occurrence_count + 1)
        start_ms = round(usable_start_ms + (latest_start_ms - usable_start_ms) * fraction)
        end_ms = min(source_duration_ms, start_ms + required_duration_ms)
        return (
            start_ms,
            end_ms,
            {
                "schema_version": "dialecticore.primer_source_analysis.v1",
                "strategy": "duration_spread_avoid_bumper",
                "source_duration_ms": source_duration_ms,
                "usable_start_ms": usable_start_ms,
                "usable_end_ms": usable_end_ms,
                "bumper_skip_ms": bumper_skip_ms,
                "tail_skip_ms": tail_skip_ms,
            },
        )

    @staticmethod
    def _visual_intent_for_beat(index: int, count: int) -> str:
        if index == 0:
            return "immediate topic hook"
        if index == count - 1:
            return "question that hands into the panel"
        if index == max(1, count // 2):
            return "central trade-off"
        return "factual evidence" if index % 2 == 0 else "context and consequence"

    @staticmethod
    def _optional_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _visual_plan_coverage(self, episode: Episode, beats: list[dict]) -> dict:
        opening_assets = {str(asset.id): asset for asset in self._opening_visual_assets(episode)}
        planner = self._visual_planner_config(episode)
        blockers: list[str] = []
        if not beats:
            blockers.append("add at least one visual beat before approving the primer")
        assigned = [beat for beat in beats if str(beat.get("asset_id") or "") in opening_assets]
        if len(assigned) != len(beats):
            blockers.append("every visual beat needs an approved source asset")
        people_free_assigned = [
            beat
            for beat in assigned
            if self._beat_is_people_free(
                beat,
                opening_assets[str(beat["asset_id"])],
            )
        ]
        if planner["exclude_people"] and len(people_free_assigned) != len(assigned):
            blockers.append(
                "every visual beat must use source media verified as people-free"
            )
        pending_manual_excerpt_count = sum(
            1
            for beat in assigned
            if beat.get("timing_source") == "pending_manual_excerpt_verification"
        )
        if pending_manual_excerpt_count:
            blockers.append(
                "verify every manually edited video excerpt before approving its sequence timing"
            )
        target_duration_ms = self._visual_plan_target_duration_ms(episode, beats)
        planned_duration_ms = sum(
            max(0, int(beat.get("duration_ms") or 0)) for beat in beats
        )
        effective_duration_by_id = {
            id(beat): duration_ms
            for beat, duration_ms in zip(
                beats,
                self._effective_visual_sequence_durations(beats, target_duration_ms),
                strict=True,
            )
        }
        rendered_duration_ms = min(planned_duration_ms, target_duration_ms)
        sequence_shortfall_ms = max(0, target_duration_ms - planned_duration_ms)
        terminal_clip_duration_ms = max(0, planned_duration_ms - target_duration_ms)
        if sequence_shortfall_ms:
            blockers.append(
                "visual sequence ends "
                f"{sequence_shortfall_ms / 1000:.1f}s before the target narration; "
                "extend a selected excerpt or add a beat"
            )
        video_beats = [
            beat
            for beat in assigned
            if opening_assets[str(beat["asset_id"])].asset_type == AssetType.video
        ]
        distinct_video_ids = {str(beat["asset_id"]) for beat in video_beats}
        video_ranges: list[tuple[str, int, int]] = []
        short_video_range_count = 0
        for beat in video_beats:
            start = self._optional_int(beat.get("source_start_ms"))
            end = self._optional_int(beat.get("source_end_ms"))
            if start is None or end is None or end <= start:
                short_video_range_count += 1
                continue
            if end - start < int(beat.get("duration_ms") or 0):
                short_video_range_count += 1
            video_ranges.append((str(beat["asset_id"]), start, end))
        if short_video_range_count:
            blockers.append("every source-video beat needs a valid selected range long enough for its shot")
        video_duration_ms = sum(
            effective_duration_by_id.get(id(beat), 0) for beat in video_beats
        )
        minimum_video_ms = 0
        asset_duration_ms: dict[str, int] = {}
        for beat in assigned:
            asset_id = str(beat["asset_id"])
            asset_duration_ms[asset_id] = asset_duration_ms.get(asset_id, 0) + effective_duration_by_id.get(
                id(beat), 0
            )
        max_asset_share = (
            max(asset_duration_ms.values(), default=0) / rendered_duration_ms
            if rendered_duration_ms
            else 0.0
        )
        max_still_duration_ms = max(
            (
                effective_duration_by_id.get(id(beat), 0)
                for beat in assigned
                if opening_assets[str(beat["asset_id"])].asset_type == AssetType.image
            ),
            default=0,
        )
        maximum_still_duration_ms = 0
        return {
            "ready": not blockers,
            "beat_count": len(beats),
            "assigned_beat_count": len(assigned),
            "people_free_assigned_beat_count": len(people_free_assigned),
            "pending_manual_excerpt_count": pending_manual_excerpt_count,
            "video_beat_count": len(video_beats),
            "distinct_video_asset_count": len(distinct_video_ids),
            "distinct_video_range_count": len(set(video_ranges)),
            "planned_visual_duration_ms": planned_duration_ms,
            "target_narration_duration_ms": target_duration_ms,
            "rendered_visual_duration_ms": rendered_duration_ms,
            "sequence_shortfall_ms": sequence_shortfall_ms,
            "terminal_clip_duration_ms": terminal_clip_duration_ms,
            "video_duration_ms": video_duration_ms,
            "minimum_video_duration_ms": minimum_video_ms,
            "video_coverage_ratio": round(video_duration_ms / rendered_duration_ms, 4)
            if rendered_duration_ms
            else 0.0,
            "max_source_asset_share": round(max_asset_share, 4),
            "max_still_duration_ms": max_still_duration_ms,
            "maximum_still_duration_ms": maximum_still_duration_ms,
            "blockers": blockers,
        }

    @staticmethod
    def _effective_visual_sequence_durations(beats: list[dict], maximum_duration_ms: int) -> list[int]:
        remaining_ms = max(0, maximum_duration_ms)
        durations: list[int] = []
        for beat in beats:
            planned_duration_ms = max(
                0, PrimerProductionService._optional_int(beat.get("duration_ms")) or 0
            )
            duration_ms = min(planned_duration_ms, remaining_ms)
            durations.append(duration_ms)
            remaining_ms -= duration_ms
        return durations

    def _visual_plan_target_duration_ms(self, episode: Episode, beats: list[dict]) -> int:
        for beat in beats:
            target_duration_ms = self._optional_int(beat.get("target_narration_duration_ms"))
            if target_duration_ms is not None and target_duration_ms > 0:
                return target_duration_ms
        return episode.definition.media.opening.target_duration_seconds * 1000

    def _visual_plan_assets(self, episode: Episode, visual_plan: dict) -> list[Asset]:
        asset_by_id = {str(asset.id): asset for asset in self._opening_visual_assets(episode)}
        planner = self._visual_planner_config(episode)
        assets: list[Asset] = []
        for beat in visual_plan.get("beats", []):
            if not isinstance(beat, dict):
                continue
            asset = asset_by_id.get(str(beat.get("asset_id") or ""))
            if asset is not None and asset not in assets:
                assets.append(asset)
        if not assets:
            raise ValueError("approved primer visual plan contains no source media")
        if planner["exclude_people"]:
            unsafe_beat = next(
                (
                    beat
                    for beat in visual_plan.get("beats", [])
                    if isinstance(beat, dict)
                    and (asset := asset_by_id.get(str(beat.get("asset_id") or ""))) is not None
                    and not self._beat_is_people_free(beat, asset)
                ),
                None,
            )
            if unsafe_beat is not None:
                raise ValueError(
                    "approved primer visual plan contains a source range not verified people-free"
                )
        return assets

    @staticmethod
    def _visual_beat_purpose(index: int, count: int) -> str:
        if index == 0:
            return "hook"
        if index == count - 1:
            return "panel_transition"
        if index == max(1, count // 2):
            return "trade_off"
        return "factual_context" if index % 2 else "evidence"

    @staticmethod
    def _script_excerpts(script: str, count: int) -> list[str]:
        tokens = script.split()
        base, remainder = divmod(len(tokens), count)
        excerpts = []
        cursor = 0
        for index in range(count):
            size = base + (1 if index < remainder else 0)
            excerpts.append(" ".join(tokens[cursor : cursor + max(1, size)]))
            cursor += max(1, size)
        return excerpts

    @staticmethod
    def _script_excerpts_for_durations(script: str, durations_ms: list[int]) -> list[str]:
        """Allocate narration in proportion to the currently displayed visual durations."""
        if not durations_ms:
            return []
        tokens = script.split()
        if not tokens:
            return [""] * len(durations_ms)
        weights = [max(0, int(duration_ms)) for duration_ms in durations_ms]
        total_weight = sum(weights)
        if total_weight <= 0:
            return PrimerProductionService._script_excerpts(script, len(durations_ms))
        excerpts: list[str] = []
        cursor = 0
        cumulative_weight = 0
        for index, weight in enumerate(weights):
            cumulative_weight += weight
            end = (
                len(tokens)
                if index == len(weights) - 1
                else round(len(tokens) * cumulative_weight / total_weight)
            )
            end = max(cursor, min(len(tokens), end))
            excerpts.append(" ".join(tokens[cursor:end]))
            cursor = end
        return excerpts

    def _opening_visual_assets(self, episode: Episode) -> list[Asset]:
        return [
            asset
            for asset in episode.assets
            if asset.status == "completed"
            and asset.asset_type in {AssetType.image, AssetType.video}
            and asset.generation_metadata.get("opening_media")
        ]

    def _visual_plan_state(self, episode: Episode) -> dict:
        state = episode.workflow_control.get("primer_visual_plan")
        if not isinstance(state, dict):
            state = {}
            episode.workflow_control["primer_visual_plan"] = state
        return state

    def _visual_plan_revisions_state(self, episode: Episode) -> list[dict]:
        revisions = episode.workflow_control.get("primer_visual_plan_revisions")
        if not isinstance(revisions, list):
            revisions = []
            episode.workflow_control["primer_visual_plan_revisions"] = revisions
        return revisions

    def _archive_visual_plan(self, episode: Episode, *, reason: str, actor: str) -> None:
        state = episode.workflow_control.get("primer_visual_plan")
        if not isinstance(state, dict) or not isinstance(state.get("beats"), list) or not state["beats"]:
            return
        snapshot = copy.deepcopy(state)
        snapshot_checksum = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        revisions = self._visual_plan_revisions_state(episode)
        if revisions and revisions[-1].get("snapshot_checksum") == snapshot_checksum:
            return
        revisions.append(
            {
                "id": f"primer-plan-revision-{uuid4().hex[:12]}",
                "created_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "actor": actor,
                "status": str(snapshot.get("status") or "not_prepared"),
                "beat_count": len(snapshot["beats"]),
                "snapshot_checksum": snapshot_checksum,
                "plan": snapshot,
            }
        )
        if len(revisions) > 30:
            del revisions[:-30]

    def _state(self, episode: Episode) -> dict:
        state = episode.workflow_control.get("primer_production")
        if not isinstance(state, dict):
            state = {}
            episode.workflow_control["primer_production"] = state
        return state

    def _asset_by_id(self, episode: Episode, asset_id: object) -> Asset | None:
        return next((asset for asset in episode.assets if str(asset.id) == str(asset_id)), None)

    @staticmethod
    def _split_duration(duration_ms: int, count: int) -> list[int]:
        base, remainder = divmod(duration_ms, max(1, count))
        return [base + (1 if index < remainder else 0) for index in range(max(1, count))]

    @staticmethod
    def _uuid_or_none(value: object):
        from uuid import UUID

        try:
            return UUID(str(value)) if value else None
        except (TypeError, ValueError):
            return None
