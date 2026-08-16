from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import mimetypes
import shutil
import struct
import subprocess
import tempfile
import wave
import zlib
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import httpx
from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Asset,
    AuditEvent,
    CharacterPerformance,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    Episode,
    QualityResult,
    SeatedCharacterReviewRequest,
    StudioPanelReviewRequest,
    TranscriptTurn,
    TranscriptVersion,
    VisualAssetPlanRequest,
    VisualCancellationRequest,
    VisualGenerationRequest,
    VisualProfile,
    VisualQualityRequest,
    VisualResultSyncRequest,
)
from app.services.model_gateway import SecretResolver
from app.services.object_storage import ObjectStore, create_object_store
from app.services.redaction import (
    is_sensitive_provider_response_key,
    safe_provider_response_payload,
)


@dataclass(frozen=True)
class VisualResult:
    status: str
    storage_uri: str | None
    mime_type: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    checksum: str | None
    metadata: dict
    media_bytes: bytes | None = None


@dataclass(frozen=True)
class VisualProbeResult:
    mime_type: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    fps: float | None
    frame_count: int | None
    size_bytes: int | None
    probe_tool: str
    probe_warnings: list[str]
    render_ready: bool
    pixel_analysis: dict | None = None
    video_analysis: dict | None = None


class _PromptContext(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ComfyUiService:
    B1_MANAGED_MEDIA_REQUIRED_PRESETS = (
        "image-default",
        "image-edit",
        "image-upscale",
        "video-text",
        "video-image",
        "talking-head-lipsync",
        "studio-seated-character-p40",
        "studio-panel-shot",
    )

    def __init__(
        self,
        settings: Settings | None = None,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport
        self.object_store = object_store or create_object_store(
            self.settings,
            secret_resolver=self.secret_resolver,
        )

    async def check_endpoint_health(self, endpoint: ComfyUiEndpoint) -> ComfyUiEndpoint:
        if endpoint.adapter_type == "mock":
            return endpoint.model_copy(
                update={
                    "health_status": "healthy",
                    "capabilities": {
                        **endpoint.capabilities,
                        "prompt": True,
                        "history": True,
                        "queue": True,
                        "image": True,
                        "video": True,
                    },
                }
            )
        if not endpoint.base_url:
            return endpoint.model_copy(update={"health_status": "unconfigured"})

        headers = {"accept": "application/json"}
        try:
            token = self.secret_resolver.resolve(endpoint.credential_reference)
        except (RuntimeError, ValueError) as exc:
            capabilities = {
                **endpoint.capabilities,
                "credential_reference_configured": bool(
                    str(endpoint.credential_reference or "").strip()
                ),
                "credential_reference_resolved": False,
                "credential_reference_error": str(exc),
            }
            return endpoint.model_copy(
                update={"health_status": "unhealthy", "capabilities": capabilities}
            )
        if token:
            headers["authorization"] = f"Bearer {token}"
        ca_path = str(endpoint.capabilities.get("tls_ca_cert_path") or "").strip()
        if ca_path and not Path(ca_path).is_file():
            capabilities = {
                **endpoint.capabilities,
                "credential_reference_configured": bool(
                    str(endpoint.credential_reference or "").strip()
                ),
                "credential_reference_resolved": bool(token),
                "tls_ca_cert_file_available": False,
            }
            return endpoint.model_copy(
                update={"health_status": "unhealthy", "capabilities": capabilities}
            )
        verify = self._endpoint_verify(endpoint)
        if endpoint.capabilities.get("native_comfyui") is True:
            health_path = "/object_info"
        else:
            health_path = "/system_stats"
        async with httpx.AsyncClient(
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
            verify=verify,
        ) as client:
            response = await client.get(
                f"{endpoint.base_url.rstrip('/')}{health_path}",
                headers=headers,
            )
            health_status = "healthy" if response.is_success else "unhealthy"
            capabilities = {
                **endpoint.capabilities,
                "prompt": True,
                "history": True,
                "queue": True,
                "credential_reference_configured": bool(
                    str(endpoint.credential_reference or "").strip()
                ),
                "credential_reference_resolved": bool(token),
            }
            if token:
                capabilities.pop("credential_reference_error", None)
            if response.is_success:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if isinstance(payload, dict):
                    if health_path == "/object_info":
                        capabilities["object_info"] = True
                        capabilities["node_metadata_count"] = len(payload)
                    else:
                        capabilities["system_stats"] = True
                        capabilities["devices"] = safe_provider_response_payload(
                            payload.get("devices", [])
                        )
            if endpoint.capabilities.get("native_comfyui") is True:
                capabilities = {
                    **capabilities,
                    "system_stats_path": "/system_stats",
                    "object_info_path": "/object_info",
                    "models_path": "/models",
                    "view_path": "/view",
                    "websocket_path": "/ws",
                    "upload_image_path": "/upload/image",
                    "upload_mask_path": "/upload/mask",
                    "interrupt_path": "/interrupt",
                    "required_read_scope": "jobs:read",
                    "required_write_scope": "jobs:write",
                }
                if response.is_success:
                    admission = await self._probe_native_prompt_admission(
                        client,
                        endpoint,
                        headers,
                    )
                    capabilities["prompt_admission_probe"] = admission
                    capabilities["prompt_admission_ready"] = bool(admission.get("ready"))
                    if not admission.get("ready"):
                        health_status = "unhealthy"
                    managed_catalog = await self._probe_b1_managed_media_catalog(
                        client,
                        endpoint,
                        headers,
                    )
                    if managed_catalog:
                        capabilities.update(managed_catalog)
            if ca_path:
                capabilities["tls_ca_cert_file_available"] = Path(ca_path).is_file()
            return endpoint.model_copy(
                update={"health_status": health_status, "capabilities": capabilities}
            )

    async def _probe_native_prompt_admission(
        self,
        client: httpx.AsyncClient,
        endpoint: ComfyUiEndpoint,
        headers: dict[str, str],
    ) -> dict[str, object]:
        prompt_headers = {**headers, "content-type": "application/json"}
        try:
            response = await client.post(
                f"{endpoint.base_url.rstrip('/')}/prompt",
                headers=prompt_headers,
                json={"prompt": {}},
            )
        except httpx.HTTPError as exc:
            return {
                "ready": False,
                "error": type(exc).__name__,
            }
        ready = response.status_code < 500 and response.status_code not in {401, 403}
        payload: object
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:500]
        return {
            "ready": ready,
            "status_code": response.status_code,
            "response": safe_provider_response_payload(payload),
        }

    async def _probe_b1_managed_media_catalog(
        self,
        client: httpx.AsyncClient,
        endpoint: ComfyUiEndpoint,
        headers: dict[str, str],
    ) -> dict[str, object]:
        api_base = str(endpoint.capabilities.get("remote_nodes_api_base") or "").strip()
        if not api_base:
            return {}
        required_presets = list(self.B1_MANAGED_MEDIA_REQUIRED_PRESETS)
        try:
            response = await client.get(
                f"{api_base.rstrip('/')}/v1/models",
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return {
                "managed_media_api": False,
                "managed_media_catalog_ready": False,
                "managed_media_catalog_error": type(exc).__name__,
                "managed_media_required_presets": required_presets,
                "managed_media_available_presets": [],
                "managed_media_missing_presets": required_presets,
            }
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        models = self._b1_managed_media_catalog_models(payload)
        available_model_ids = sorted(
            {
                model_id
                for model in models
                if (model_id := str(model.get("id") or model.get("model") or "").strip())
                and self._b1_managed_media_model_available(model)
            }
        )
        available_presets = sorted(
            {
                model_id
                for model_id in available_model_ids
                if model_id in self.B1_MANAGED_MEDIA_REQUIRED_PRESETS
            }
        )
        missing_presets = [
            preset for preset in required_presets if preset not in set(available_presets)
        ]
        catalog_ready = response.is_success and not missing_presets
        result: dict[str, object] = {
            "managed_media_api": response.is_success,
            "managed_media_catalog_ready": catalog_ready,
            "managed_media_catalog_status_code": response.status_code,
            "managed_media_model_count": len(models),
            "managed_media_required_presets": required_presets,
            "managed_media_available_presets": available_presets,
            "managed_media_available_model_ids": available_model_ids,
            "managed_media_missing_presets": missing_presets,
            "managed_media_models_path": "/v1/models",
        }
        unavailable = [
            safe_provider_response_payload(
                {
                    "id": model.get("id") or model.get("model"),
                    "status": model.get("status"),
                    "enabled": model.get("enabled"),
                    "reason": model.get("reason") or model.get("message"),
                }
            )
            for model in models
            if str(model.get("id") or model.get("model") or "").strip() in required_presets
            and not self._b1_managed_media_model_available(model)
        ]
        if unavailable:
            result["managed_media_unavailable_presets"] = unavailable
        if not response.is_success:
            result["managed_media_catalog_response"] = safe_provider_response_payload(payload)
        return result

    def _b1_managed_media_catalog_models(self, payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _b1_managed_media_model_available(self, model: dict) -> bool:
        if model.get("enabled") is False or model.get("available") is False:
            return False
        status = str(model.get("status") or model.get("state") or "").strip().lower()
        if status in {"disabled", "missing", "unavailable", "failed", "error"}:
            return False
        return True

    async def bootstrap_ca_certificate(self, endpoint: ComfyUiEndpoint) -> ComfyUiEndpoint:
        bootstrap_url = str(endpoint.capabilities.get("ca_cert_bootstrap_url") or "").strip()
        if not bootstrap_url:
            raise ValueError("comfyui endpoint has no CA bootstrap URL")

        async with httpx.AsyncClient(
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
            verify=False,
        ) as client:
            response = await client.get(bootstrap_url, headers={"accept": "application/x-pem-file"})
            response.raise_for_status()
            certificate = response.content

        actual_sha256 = hashlib.sha256(certificate).hexdigest()
        expected_sha256 = str(endpoint.capabilities.get("ca_cert_sha256") or "").strip()
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            raise ValueError("downloaded CA certificate SHA-256 does not match endpoint capability")

        storage_path = self._ca_certificate_storage_path(endpoint)
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_bytes(certificate)
        capabilities = {
            **endpoint.capabilities,
            "tls_ca_cert_path": str(storage_path),
            "ca_cert_bootstrap": {
                "stored": True,
                "sha256_matches": not expected_sha256 or actual_sha256.lower()
                == expected_sha256.lower(),
            },
        }
        return endpoint.model_copy(update={"capabilities": capabilities})

    def store_visual_profile_reference_image(
        self,
        profile: VisualProfile,
        *,
        filename: str,
        content_type: str,
        image_base64: str,
        reference_type: str = "portrait",
    ) -> dict:
        mime_type = self._reference_image_mime_type(content_type)
        try:
            payload = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("reference image payload must be valid base64") from exc
        if not payload:
            raise ValueError("reference image payload must not be empty")
        self._validate_reference_image_payload(payload, mime_type)
        extension = self._reference_image_extension(mime_type, filename)
        checksum = hashlib.sha256(payload).hexdigest()
        stored = self.object_store.put_bytes(
            key=(
                f"visual-profiles/{profile.id}/reference-images/"
                f"{reference_type}/{checksum[:16]}{extension}"
            ),
            payload=payload,
            content_type=mime_type,
        )
        return {
            "reference_image_type": reference_type,
            "reference_image_uri": stored.uri,
            "reference_image_checksum": stored.checksum,
            "reference_image_size_bytes": stored.size_bytes,
            "reference_image_content_type": stored.content_type,
            "reference_image_object_key": stored.key,
        }

    def store_scene_reference_image(
        self,
        *,
        filename: str,
        content_type: str,
        image_base64: str,
    ) -> dict:
        mime_type = self._reference_image_mime_type(content_type)
        try:
            payload = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("scene reference image payload must be valid base64") from exc
        if not payload:
            raise ValueError("scene reference image payload must not be empty")
        self._validate_reference_image_payload(payload, mime_type)
        extension = self._reference_image_extension(mime_type, filename)
        checksum = hashlib.sha256(payload).hexdigest()
        stored = self.object_store.put_bytes(
            key=f"show-media/scene-reference-images/{checksum[:16]}{extension}",
            payload=payload,
            content_type=mime_type,
        )
        return {
            "scene_reference_image_uri": stored.uri,
            "content_type": stored.content_type,
            "checksum": stored.checksum,
            "size_bytes": stored.size_bytes,
            "object_key": stored.key,
        }

    def plan_visual_assets(
        self,
        episode: Episode,
        request: VisualAssetPlanRequest,
        visual_profiles: list[VisualProfile],
        workflows: list[ComfyUiWorkflow],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            raise ValueError("target transcript has no playable turns")

        visual_by_id = {profile.id: profile for profile in visual_profiles if profile.enabled}
        workflow_by_id = {workflow.id: workflow for workflow in workflows if workflow.enabled}
        participant_by_id = {participant.id: participant for participant in episode.participants}

        if self._uses_seated_panel(episode):
            return self._plan_seated_panel_assets(
                episode=episode,
                request=request,
                transcript=transcript,
                playable_turns=playable_turns,
                visual_by_id=visual_by_id,
                workflow_by_id=workflow_by_id,
                participant_by_id=participant_by_id,
            )

        if request.regenerate:
            for asset in episode.assets:
                if (
                    asset.language == transcript.language
                    and asset.asset_type
                    in {
                        AssetType.video,
                        AssetType.broll,
                        AssetType.reaction_loop,
                        AssetType.studio_scene,
                        AssetType.citation_card,
                    }
                    and asset.status != "replaced"
                ):
                    asset.status = "replaced"
                    asset.updated_at = datetime.now(UTC)

        planned_count = 0
        skipped_count = 0
        broll_count = 0
        citation_card_count = 0
        reusable_count = 0
        reaction_loop_count = 0
        studio_scene_count = 0
        studio_group_cutaway_count = 0
        directing = episode.definition.media.directing
        studio_workflow = self._workflow_by_type(workflow_by_id, "studio_wide_shot")
        evidence_sources_by_id = self._latest_evidence_sources_by_id(episode)
        if (
            directing.mode == "studio_directed"
            and directing.require_generated_studio
            and studio_workflow is not None
            and not self._has_active_reusable_visual_asset(
            episode,
            transcript,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            visual_role="studio_scene",
            )
        ):
            episode.assets.append(
                self._planned_reusable_asset(
                    episode,
                    transcript,
                    source_entity_type="episode",
                    source_entity_id=str(episode.id),
                    asset_type=AssetType.studio_scene,
                    visual_role="studio_scene",
                    shot_type="studio_wide_establishing",
                    workflow=studio_workflow,
                    visual_profile=None,
                    prompt_text="Reusable wide studio establishing shot",
                )
            )
            planned_count += 1
            reusable_count += 1
            studio_scene_count += 1

        active_participant_ids = {
            turn.speaker_participant_id for turn in playable_turns if turn.speaker_participant_id
        }
        group_reference_image_uris = self._group_reference_image_uris(
            active_participant_ids,
            participant_by_id,
            visual_by_id,
        )
        if (
            directing.mode == "studio_directed"
            and directing.require_group_cutaways
            and studio_workflow is not None
            and not self._has_active_reusable_visual_asset(
                episode,
                transcript,
                source_entity_type="episode",
                source_entity_id=f"{episode.id}:group",
                visual_role="studio_group_cutaway",
            )
        ):
            episode.assets.append(
                self._planned_reusable_asset(
                    episode,
                    transcript,
                    source_entity_type="episode",
                    source_entity_id=f"{episode.id}:group",
                    asset_type=AssetType.studio_scene,
                    visual_role="studio_group_cutaway",
                    shot_type="studio_panel_two_shot",
                    workflow=studio_workflow,
                    visual_profile=None,
                    prompt_text=(
                        "Silent panel two-shot cutaway in the configured studio. "
                        "The panel is listening between spoken turns; no visible speech."
                    ),
                    additional_prompt_inputs={
                        "group_reference_image_uris": group_reference_image_uris,
                        "group_reference_character_count": len(group_reference_image_uris),
                    },
                )
            )
            planned_count += 1
            reusable_count += 1
            studio_group_cutaway_count += 1
        for participant_id in sorted(active_participant_ids):
            if not (
                directing.mode == "studio_directed"
                and directing.require_reaction_cutaways
            ):
                continue
            participant = participant_by_id.get(participant_id)
            visual_profile = (
                visual_by_id.get(participant.visual_profile_id)
                if participant and participant.visual_profile_id
                else None
            )
            if visual_profile is None or not visual_profile.reaction_workflow_id:
                continue
            reaction_workflow = workflow_by_id.get(visual_profile.reaction_workflow_id)
            if reaction_workflow is None:
                continue
            if self._has_active_reusable_visual_asset(
                episode,
                transcript,
                source_entity_type="participant_profile",
                source_entity_id=participant_id,
                visual_role="reaction_loop",
            ):
                continue
            episode.assets.append(
                self._planned_reusable_asset(
                    episode,
                    transcript,
                    source_entity_type="participant_profile",
                    source_entity_id=participant_id,
                    asset_type=AssetType.reaction_loop,
                    visual_role="reaction_loop",
                    shot_type="listening_loop",
                    workflow=reaction_workflow,
                    visual_profile=visual_profile,
                    prompt_text=f"{visual_profile.character_name} reusable listening loop",
                )
            )
            planned_count += 1
            reusable_count += 1
            reaction_loop_count += 1

        for index, turn in enumerate(playable_turns, start=1):
            participant = participant_by_id.get(turn.speaker_participant_id)
            visual_profile = (
                visual_by_id.get(participant.visual_profile_id)
                if participant and participant.visual_profile_id
                else None
            )
            if visual_profile is None:
                skipped_count += 1
                continue

            workflow = workflow_by_id.get(visual_profile.primary_workflow_id)
            if workflow is None:
                skipped_count += 1
                continue

            if not self._has_active_visual_asset(
                episode,
                transcript,
                str(turn.id),
                visual_role="video_primary",
            ):
                episode.assets.append(
                    self._planned_asset(
                        episode,
                        transcript,
                        str(turn.id),
                        AssetType.video,
                        visual_role="video_primary",
                        shot_type=self._primary_shot_type(index, turn.speaker_participant_id),
                        visual_profile=visual_profile,
                        workflow=workflow,
                        transcript_text=turn.text,
                        duration_ms=self._turn_audio_duration_ms(episode, transcript, str(turn.id)),
                    )
                )
                planned_count += 1

            if (
                episode.definition.media.generate_broll
                and directing.broll_policy == "contextual_only"
                and index % 3 == 0
                and visual_profile.broll_workflow_id
            ):
                broll_workflow = workflow_by_id.get(visual_profile.broll_workflow_id)
                if broll_workflow is not None and not self._has_active_visual_asset(
                    episode,
                    transcript,
                    str(turn.id),
                    visual_role="broll",
                ):
                    episode.assets.append(
                        self._planned_asset(
                            episode,
                            transcript,
                            str(turn.id),
                            AssetType.broll,
                            visual_role="broll",
                            shot_type="topic_broll",
                            visual_profile=visual_profile,
                            workflow=broll_workflow,
                            transcript_text=turn.text,
                            duration_ms=self._turn_audio_duration_ms(
                                episode,
                                transcript,
                                str(turn.id),
                            ),
                        )
                    )
                    planned_count += 1
                    broll_count += 1

            self._apply_turn_shot_plan(episode, transcript, turn, index)
            citation_entries = self._citation_entries_for_turn(turn, evidence_sources_by_id)
            if (
                episode.definition.media.generate_citation_cards
                and episode.definition.media.evidence_presentation == "burned_overlays"
                and citation_entries
                and not self._has_active_visual_asset(
                    episode,
                    transcript,
                    str(turn.id),
                    visual_role="citation_overlay",
                )
            ):
                episode.assets.append(
                    self._planned_citation_card_asset(
                        episode=episode,
                        transcript=transcript,
                        turn=turn,
                        citation_entries=citation_entries,
                        duration_ms=self._turn_audio_duration_ms(
                            episode,
                            transcript,
                            str(turn.id),
                        ),
                    )
                )
                planned_count += 1
                citation_card_count += 1

        qc = self._visual_plan_qc(
            episode,
            transcript,
            visual_profiles=visual_profiles,
            workflows=workflows,
        )
        episode.quality_results.append(qc)
        episode.status = EpisodeStatus.ready
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.assets.planned",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "planned_count": planned_count,
                    "skipped_count": skipped_count,
                    "broll_count": broll_count,
                    "citation_card_count": citation_card_count,
                    "reusable_count": reusable_count,
                    "reaction_loop_count": reaction_loop_count,
                    "studio_scene_count": studio_scene_count,
                    "studio_group_cutaway_count": studio_group_cutaway_count,
                    "directing": directing.model_dump(mode="json"),
                    "regenerate": request.regenerate,
                    "visual_plan_qc_status": qc.status,
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def review_studio_panel_keyframe(
        self,
        episode: Episode,
        asset_id: str,
        request: StudioPanelReviewRequest,
    ) -> Episode:
        """Record a human decision for the master every seated clip depends on."""
        asset = next(
            (candidate for candidate in episode.assets if str(candidate.id) == asset_id),
            None,
        )
        if asset is None:
            raise ValueError("studio panel master was not found")
        if asset.generation_metadata.get("visual_role") != "studio_panel_keyframe":
            raise ValueError("only a studio panel master can be reviewed here")
        if asset.status != "completed":
            raise ValueError("studio panel master must be completed before review")
        if asset.generation_metadata.get("adapter") != "b1_managed_media":
            raise ValueError("studio panel master must be a B1 managed-media result")
        studio_panel = asset.generation_metadata.get("studio_panel")
        if not isinstance(studio_panel, dict):
            raise ValueError("studio panel master is missing B1 studio evidence")
        if request.decision == "approved" and not str(
            studio_panel.get("scene_artifact_id") or ""
        ).strip():
            raise ValueError("studio panel master is missing its reusable B1 scene reference")

        reviewed_at = datetime.now(UTC)
        asset.generation_metadata = {
            **asset.generation_metadata,
            "approval_status": request.decision,
            "reviewed_at": reviewed_at.isoformat(),
            "reviewed_by": request.user_id or "web-ui",
            "review_comment": request.comment.strip() if request.comment else None,
        }
        asset.updated_at = reviewed_at
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.studio_panel.reviewed",
                actor=request.user_id or "web-ui",
                details={
                    "asset_id": str(asset.id),
                    "decision": request.decision,
                    "comment": request.comment.strip() if request.comment else None,
                    "scene_artifact_id": studio_panel.get("scene_artifact_id"),
                },
            )
        )
        episode.updated_at = reviewed_at
        return episode

    async def review_seated_character(
        self,
        episode: Episode,
        asset_id: str,
        request: SeatedCharacterReviewRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
    ) -> Episode:
        """Review a seated plate and bind approval to B1's owner-scoped job."""
        asset = next(
            (candidate for candidate in episode.assets if str(candidate.id) == asset_id),
            None,
        )
        if asset is None:
            raise ValueError("seated-character plate was not found")
        if asset.generation_metadata.get("visual_role") != "studio_seated_character":
            raise ValueError("only a seated-character plate can be reviewed here")
        if asset.status != "completed":
            raise ValueError("seated-character plate must be completed before review")
        if asset.generation_metadata.get("adapter") != "b1_managed_media":
            raise ValueError("seated-character plate must be a B1 managed-media result")

        remote_job_id = str(asset.generation_metadata.get("remote_job_id") or "").strip()
        if not remote_job_id:
            raise ValueError("seated-character plate is missing its B1 job id")
        provider_response: dict = {}
        if request.decision == "approved":
            workflow_by_id = {workflow.id: workflow for workflow in workflows}
            workflow = self._workflow_for_asset(asset, workflow_by_id)
            endpoint = next(
                (
                    candidate
                    for candidate in endpoints
                    if candidate.id == workflow.comfyui_endpoint_id
                ),
                None,
            )
            if endpoint is None:
                raise ValueError("seated-character workflow endpoint was not found")
            api_base = self._remote_job_api_base(endpoint, asset).rstrip("/")
            token = self.secret_resolver.resolve(endpoint.credential_reference)
            headers = {"accept": "application/json"}
            if token:
                headers["authorization"] = f"Bearer {token}"
            timeout = httpx.Timeout(endpoint.default_timeout_seconds)
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=timeout,
                headers=headers,
                verify=self._endpoint_verify(endpoint),
            ) as client:
                response = await client.post(
                    f"{api_base}/admin/jobs/{remote_job_id}/approve-seated-character"
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    detail = self._safe_provider_response_payload(response.json())
                except ValueError:
                    detail = response.text[:500]
                raise ValueError(
                    "B1 seated-character approval failed with HTTP "
                    f"{response.status_code}: {detail}"
                ) from exc
            if response.content:
                try:
                    raw_response = response.json()
                except ValueError:
                    raw_response = {}
                if isinstance(raw_response, dict):
                    provider_response = self._safe_provider_response_payload(raw_response)

        reviewed_at = datetime.now(UTC)
        asset.generation_metadata = {
            **asset.generation_metadata,
            "approval_status": request.decision,
            "reviewed_at": reviewed_at.isoformat(),
            "reviewed_by": request.user_id or "web-ui",
            "review_comment": request.comment.strip() if request.comment else None,
            **(
                {"b1_approval_response": provider_response}
                if request.decision == "approved"
                else {}
            ),
        }
        asset.updated_at = reviewed_at
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.seated_character.reviewed",
                actor=request.user_id or "web-ui",
                details={
                    "asset_id": str(asset.id),
                    "participant_id": asset.source_entity_id,
                    "remote_job_id": remote_job_id,
                    "decision": request.decision,
                    "comment": request.comment.strip() if request.comment else None,
                },
            )
        )
        episode.updated_at = reviewed_at
        return episode

    @staticmethod
    def _uses_seated_panel(episode: Episode) -> bool:
        directing = episode.definition.media.directing
        return (
            directing.mode == "studio_directed"
            and directing.studio_layout == "seated_panel"
        )

    def _plan_seated_panel_assets(
        self,
        *,
        episode: Episode,
        request: VisualAssetPlanRequest,
        transcript: TranscriptVersion,
        playable_turns: list[TranscriptTurn],
        visual_by_id: dict[str, VisualProfile],
        workflow_by_id: dict[str, ComfyUiWorkflow],
        participant_by_id: dict[str, object],
    ) -> Episode:
        """Plan a coherent episode-level set before scheduling seat-bound speech clips."""
        directing = episode.definition.media.directing
        seated_character_workflow = self._workflow_by_type(
            workflow_by_id, "studio_seated_character"
        )
        panel_workflow = self._workflow_by_type(workflow_by_id, "studio_panel_shot")
        lipsync_workflow = self._workflow_by_type(workflow_by_id, "seated_panel_lipsync")
        active_participant_ids = {
            turn.speaker_participant_id for turn in playable_turns if turn.speaker_participant_id
        }
        seating = self._resolved_seating_plan(
            episode=episode,
            participant_ids=active_participant_ids,
            participant_by_id=participant_by_id,
        )
        panel_participants = self._panel_participant_references(
            seating=seating,
            participant_by_id=participant_by_id,
            visual_by_id=visual_by_id,
        )

        if request.regenerate:
            for asset in episode.assets:
                if (
                    asset.language == transcript.language
                    and asset.status != "replaced"
                    and (
                        asset.generation_metadata.get("transcript_version_id")
                        == str(transcript.id)
                        or (
                            asset.generation_metadata.get("transcript_version_id") is None
                            and asset.generation_metadata.get("visual_role")
                            in {"studio_seated_character", "studio_panel_keyframe"}
                        )
                    )
                    and asset.generation_metadata.get("visual_role")
                    in {
                        "video_primary",
                        "broll",
                        "wall_screen_broll",
                        "studio_seated_character",
                        "studio_panel_keyframe",
                    }
                ):
                    asset.status = "replaced"
                    asset.updated_at = datetime.now(UTC)

        planned_count = 0
        broll_count = 0
        coverage_count = 0
        seated_character_count = 0
        skipped_count = 0
        seated_character_by_participant: dict[str, Asset] = {}
        studio_reference_uri = episode.definition.media.scene_reference_image_uri
        if seated_character_workflow is not None and studio_reference_uri:
            for participant_reference in panel_participants:
                participant_id = str(participant_reference.get("participant_id") or "")
                seat = participant_reference.get("seat")
                participant = participant_by_id.get(participant_id)
                visual_profile_id = getattr(participant, "visual_profile_id", None)
                visual_profile = (
                    visual_by_id.get(visual_profile_id) if visual_profile_id else None
                )
                if not participant_id or not isinstance(seat, int) or visual_profile is None:
                    continue
                existing_plate = self._active_seated_character_asset(
                    episode=episode,
                    transcript=transcript,
                    participant_id=participant_id,
                    seat=seat,
                    studio_reference_uri=studio_reference_uri,
                    portrait_reference_uri=str(
                        participant_reference.get("portrait_reference_image_uri") or ""
                    ),
                    full_body_reference_uri=str(
                        participant_reference.get("full_body_reference_image_uri") or ""
                    ),
                    workflow_id=seated_character_workflow.id,
                )
                if existing_plate is None:
                    for stale_plate in episode.assets:
                        if (
                            stale_plate.language == transcript.language
                            and stale_plate.status != "replaced"
                            and stale_plate.source_entity_type == "participant_profile"
                            and stale_plate.source_entity_id == participant_id
                            and stale_plate.generation_metadata.get("visual_role")
                            == "studio_seated_character"
                            and stale_plate.generation_metadata.get("transcript_version_id")
                            == str(transcript.id)
                            and stale_plate.generation_metadata.get("prompt_inputs", {}).get(
                                "seat"
                            )
                            == seat
                        ):
                            stale_plate.status = "replaced"
                            stale_plate.updated_at = datetime.now(UTC)
                    existing_plate = self._planned_reusable_asset(
                        episode,
                        transcript,
                        source_entity_type="participant_profile",
                        source_entity_id=participant_id,
                        asset_type=AssetType.image,
                        visual_role="studio_seated_character",
                        shot_type="neutral_seated_plate",
                        workflow=seated_character_workflow,
                        visual_profile=visual_profile,
                        prompt_text=(
                            f"{visual_profile.character_name} seated naturally in studio "
                            f"seat {seat}, identity and wardrobe preserved."
                        ),
                        additional_prompt_inputs={
                            "studio_layout": "seated_panel",
                            "participant_id": participant_id,
                            "seat": seat,
                            "pose": "neutral_seated",
                            "camera_view": "establishing_wide",
                            "camera_angle": "front_three_quarter",
                            "show_scene_reference_image_uri": studio_reference_uri,
                        },
                    )
                    episode.assets.append(existing_plate)
                    seated_character_count += 1
                seated_character_by_participant[participant_id] = existing_plate

        panel_participants_with_plates = [
            {
                **participant_reference,
                "seated_character_asset_id": str(
                    seated_character_by_participant[
                        str(participant_reference.get("participant_id") or "")
                    ].id
                ),
            }
            for participant_reference in panel_participants
            if str(participant_reference.get("participant_id") or "")
            in seated_character_by_participant
        ]
        stature_reference_participant_id = (
            "claude"
            if "claude" in seated_character_by_participant
            else next(iter(seating), None)
        )
        seated_character_dependency_ids = [
            str(asset.id)
            for _, asset in sorted(
                seated_character_by_participant.items(),
                key=lambda item: int(
                    item[1].generation_metadata.get("prompt_inputs", {}).get("seat") or 0
                ),
            )
        ]
        coverage_by_key: dict[str, Asset] = {}
        master_coverage_key = self._panel_coverage_key("establishing_wide", None, [])
        if panel_workflow is not None:
            for view, participant_id, paired_ids in self._required_panel_coverages(
                playable_turns=playable_turns,
                seating=seating,
            ):
                coverage_key = self._panel_coverage_key(view, participant_id, paired_ids)
                existing = self._active_panel_keyframe_asset(
                    episode=episode,
                    transcript=transcript,
                    coverage_key=coverage_key,
                )
                if existing is not None and (
                    existing.generation_metadata.get("comfyui_workflow_id")
                    != panel_workflow.id
                    or existing.generation_metadata.get("prompt_inputs", {}).get(
                        "depends_on_asset_ids"
                    )
                    != seated_character_dependency_ids
                ):
                    existing.status = "replaced"
                    existing.updated_at = datetime.now(UTC)
                    existing = None
                if existing is not None:
                    coverage_by_key[coverage_key] = existing
                    continue
                asset = self._planned_reusable_asset(
                    episode,
                    transcript,
                    source_entity_type="episode",
                    source_entity_id=f"{episode.id}:panel:{coverage_key}",
                    asset_type=AssetType.studio_scene,
                    visual_role="studio_panel_keyframe",
                    shot_type=view,
                    workflow=panel_workflow,
                    visual_profile=None,
                    prompt_text=(
                        "Identity-preserving seated panel coverage. All participants remain in "
                        "their assigned seats around the studio table."
                    ),
                    additional_prompt_inputs={
                        "studio_layout": "seated_panel",
                        "panel_coverage_key": coverage_key,
                        "camera_view": view,
                        "speaker_participant_id": participant_id,
                        "paired_participant_ids": paired_ids,
                        "seating_plan": seating,
                        "stature_reference_participant_id": (
                            stature_reference_participant_id
                        ),
                        "panel_participants": panel_participants_with_plates,
                        "depends_on_asset_ids": seated_character_dependency_ids,
                    },
                )
                episode.assets.append(asset)
                coverage_by_key[coverage_key] = asset
                planned_count += 1
                coverage_count += 1

        for index, turn in enumerate(playable_turns, start=1):
            participant = participant_by_id.get(turn.speaker_participant_id)
            visual_profile = (
                visual_by_id.get(getattr(participant, "visual_profile_id", None))
                if participant is not None and getattr(participant, "visual_profile_id", None)
                else None
            )
            if visual_profile is None or lipsync_workflow is None:
                skipped_count += 1
                continue

            wall_screen_asset: Asset | None = None
            has_wall_broll = (
                episode.definition.media.generate_broll
                and directing.broll_policy == "contextual_only"
                and directing.broll_presentation == "wall_screen_only"
                and index % 3 == 0
                and bool(visual_profile.broll_workflow_id)
            )
            if has_wall_broll:
                broll_workflow = workflow_by_id.get(visual_profile.broll_workflow_id)
                if broll_workflow is not None:
                    wall_screen_asset = self._active_visual_asset(
                        episode,
                        transcript,
                        str(turn.id),
                        visual_role="wall_screen_broll",
                    )
                    if wall_screen_asset is None:
                        wall_screen_asset = self._planned_asset(
                            episode,
                            transcript,
                            str(turn.id),
                            AssetType.broll,
                            visual_role="wall_screen_broll",
                            shot_type="wall_screen_editorial_insert",
                            visual_profile=visual_profile,
                            workflow=broll_workflow,
                            transcript_text=turn.text,
                            duration_ms=self._turn_audio_duration_ms(
                                episode, transcript, str(turn.id)
                            ),
                            additional_prompt_inputs={
                                "presentation": "rear_studio_wall_screen_only",
                            },
                        )
                        episode.assets.append(wall_screen_asset)
                        planned_count += 1
                        broll_count += 1

            camera_view, camera_action, paired_ids = self._seated_panel_directing_decision(
                turn_index=index,
                speaker_participant_id=turn.speaker_participant_id,
                seating=seating,
                turn_type=turn.turn_type.value if turn.turn_type is not None else None,
            )
            # B1 uses this high-resolution establishing-wide master as the
            # private scene reference for every native per-turn camera shot.
            scene_asset = coverage_by_key.get(master_coverage_key)
            if scene_asset is None:
                skipped_count += 1
                continue
            # The shared panel has no foreground/depth mask for the rear
            # screen. Embedding a card before lipsync paints over seated bodies
            # and the desk, after which only the small face patch is restored.
            # Keep the source-bound card as a separate editorial asset until a
            # masked compositor exists; never pass it as the lipsync canvas.
            lipsync_wall_screen_asset: Asset | None = None
            primary = self._active_visual_asset(
                episode,
                transcript,
                str(turn.id),
                visual_role="video_primary",
            )
            if primary is not None:
                existing_prompt_inputs = primary.generation_metadata.get("prompt_inputs")
                existing_scene_asset_id = (
                    existing_prompt_inputs.get("scene_keyframe_asset_id")
                    if isinstance(existing_prompt_inputs, dict)
                    else None
                )
                existing_camera_view = (
                    existing_prompt_inputs.get("camera_view")
                    if isinstance(existing_prompt_inputs, dict)
                    else None
                )
                existing_wall_screen_asset_id = (
                    existing_prompt_inputs.get("wall_screen_broll_asset_id")
                    if isinstance(existing_prompt_inputs, dict)
                    else None
                )
                if (
                    existing_scene_asset_id != str(scene_asset.id)
                    or existing_camera_view != camera_view
                    or existing_wall_screen_asset_id is not None
                ):
                    # A former panel coverage cannot be silently reused with a
                    # new master plate or an obsolete directing decision.
                    # Retire it and create a correctly bound clip below.
                    primary.status = "replaced"
                    primary.updated_at = datetime.now(UTC)
                    primary = None
            if primary is None:
                dependencies = [str(scene_asset.id)]
                primary = self._planned_asset(
                    episode,
                    transcript,
                    str(turn.id),
                    AssetType.video,
                    visual_role="video_primary",
                    shot_type="seated_panel_lipsync",
                    visual_profile=visual_profile,
                    workflow=lipsync_workflow,
                    transcript_text=turn.text,
                    duration_ms=self._turn_audio_duration_ms(episode, transcript, str(turn.id)),
                    additional_prompt_inputs={
                        "studio_layout": "seated_panel",
                        "seating_plan": seating,
                        "panel_participants": panel_participants,
                        "camera_view": camera_view,
                        "camera_action": camera_action,
                        "speaker_participant_id": turn.speaker_participant_id,
                        "paired_participant_ids": paired_ids,
                        "scene_keyframe_asset_id": str(scene_asset.id),
                        "wall_screen_broll_asset_id": None,
                        "depends_on_asset_ids": dependencies,
                    },
                )
                episode.assets.append(primary)
                planned_count += 1
            self._apply_seated_panel_turn_shot_plan(
                primary=primary,
                turn_index=index,
                camera_view=camera_view,
                camera_action=camera_action,
                scene_asset=scene_asset,
                wall_screen_asset=lipsync_wall_screen_asset,
                seating=seating,
                paired_ids=paired_ids,
            )

        qc = self._visual_plan_qc(
            episode,
            transcript,
            list(visual_by_id.values()),
            list(workflow_by_id.values()),
        )
        episode.quality_results.append(qc)
        episode.status = EpisodeStatus.ready
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.assets.seated_panel_planned",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "planned_count": planned_count,
                    "skipped_count": skipped_count,
                    "panel_keyframe_count": coverage_count,
                    "seated_character_count": seated_character_count,
                    "seated_character_asset_ids": seated_character_dependency_ids,
                    "wall_screen_broll_count": broll_count,
                    "seating_plan": seating,
                    "visual_plan_qc_status": qc.status,
                    "regenerate": request.regenerate,
                },
            )
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.ready.value},
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def _resolved_seating_plan(
        self,
        *,
        episode: Episode,
        participant_ids: set[str],
        participant_by_id: dict[str, object],
    ) -> dict[str, int]:
        """Resolve a stable, editable seat map with the moderator in the centre slot."""
        role_by_id = {
            assignment.participant_profile_id: assignment.role.casefold()
            for assignment in episode.definition.participants
        }
        ordered_ids = sorted(
            participant_ids,
            key=lambda participant_id: (
                0
                if role_by_id.get(participant_id) in {"moderator", "host"}
                else 1,
                str(
                    getattr(participant_by_id.get(participant_id), "display_name", participant_id)
                ).casefold(),
                participant_id,
            ),
        )
        configured = {
            participant_id: seat
            for participant_id, seat in episode.definition.media.directing.seating_plan.items()
            if participant_id in participant_ids
        }
        available_seats = list(range(1, len(ordered_ids) + 1))
        remaining_seats = [seat for seat in available_seats if seat not in configured.values()]
        host_id = next(
            (
                participant_id
                for participant_id in ordered_ids
                if role_by_id.get(participant_id) in {"moderator", "host"}
                and participant_id not in configured
            ),
            None,
        )
        if host_id is not None and remaining_seats:
            centre = (len(available_seats) + 1) // 2
            configured[host_id] = centre if centre in remaining_seats else remaining_seats[0]
            remaining_seats.remove(configured[host_id])
        for participant_id in ordered_ids:
            if participant_id not in configured and remaining_seats:
                configured[participant_id] = remaining_seats.pop(0)
        return dict(sorted(configured.items(), key=lambda item: item[1]))

    def _panel_participant_references(
        self,
        *,
        seating: dict[str, int],
        participant_by_id: dict[str, object],
        visual_by_id: dict[str, VisualProfile],
    ) -> list[dict]:
        result: list[dict] = []
        for participant_id, seat in seating.items():
            participant = participant_by_id.get(participant_id)
            visual_profile_id = getattr(participant, "visual_profile_id", None)
            visual_profile = visual_by_id.get(visual_profile_id) if visual_profile_id else None
            if visual_profile is None:
                continue
            reference_images = {
                reference.reference_type: reference.uri
                for reference in visual_profile.reference_images
                if reference.uri
            }
            result.append(
                {
                    "participant_id": participant_id,
                    "character_name": visual_profile.character_name,
                    "seat": seat,
                    "portrait_reference_image_uri": reference_images.get("portrait"),
                    "full_body_reference_image_uri": reference_images.get("full_body"),
                    "wardrobe_reference_image_uri": reference_images.get("wardrobe"),
                    "visual_profile_id": visual_profile.id,
                }
            )
        return result

    def _required_panel_coverages(
        self,
        *,
        playable_turns: list[TranscriptTurn],
        seating: dict[str, int],
    ) -> list[tuple[str, str | None, list[str]]]:
        if not playable_turns or not seating:
            return []
        # B1 creates one high-resolution establishing-wide master per episode.
        # Each dialogue turn requests native coverage from that private scene
        # reference, preserving a single seated studio across the episode.
        return [("establishing_wide", None, [])]

    @staticmethod
    def _panel_coverage_key(
        view: str,
        participant_id: str | None,
        paired_ids: list[str],
    ) -> str:
        focus = participant_id or "panel"
        paired = "+".join(sorted(paired_ids)) or "solo"
        return f"{view}:{focus}:{paired}"

    def _active_panel_keyframe_asset(
        self,
        *,
        episode: Episode,
        transcript: TranscriptVersion,
        coverage_key: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.language == transcript.language
                and asset.asset_type == AssetType.studio_scene
                and asset.status != "replaced"
                and asset.generation_metadata.get("visual_role") == "studio_panel_keyframe"
                and asset.generation_metadata.get("prompt_inputs", {}).get("panel_coverage_key")
                == coverage_key
                and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            ),
            None,
        )

    def _active_seated_character_asset(
        self,
        *,
        episode: Episode,
        transcript: TranscriptVersion,
        participant_id: str,
        seat: int,
        studio_reference_uri: str,
        portrait_reference_uri: str,
        full_body_reference_uri: str,
        workflow_id: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.language == transcript.language
                and asset.asset_type == AssetType.image
                and asset.status != "replaced"
                and asset.source_entity_type == "participant_profile"
                and asset.source_entity_id == participant_id
                and asset.generation_metadata.get("visual_role")
                == "studio_seated_character"
                and asset.generation_metadata.get("transcript_version_id")
                == str(transcript.id)
                and asset.generation_metadata.get("prompt_inputs", {}).get("seat") == seat
                and asset.generation_metadata.get("prompt_inputs", {}).get(
                    "show_scene_reference_image_uri"
                )
                == studio_reference_uri
                and asset.generation_metadata.get("prompt_inputs", {}).get(
                    "portrait_reference_image_uri"
                )
                == portrait_reference_uri
                and asset.generation_metadata.get("prompt_inputs", {}).get(
                    "full_body_reference_image_uri"
                )
                == full_body_reference_uri
                and asset.generation_metadata.get("comfyui_workflow_id") == workflow_id
            ),
            None,
        )

    @staticmethod
    def _seated_panel_directing_decision(
        *,
        turn_index: int,
        speaker_participant_id: str,
        seating: dict[str, int],
        turn_type: str | None,
    ) -> tuple[str, str, list[str]]:
        ordered_ids = [
            participant_id
            for participant_id, _ in sorted(seating.items(), key=lambda item: item[1])
        ]
        speaker_index = (
            ordered_ids.index(speaker_participant_id)
            if speaker_participant_id in ordered_ids
            else 0
        )
        paired_ids: list[str] = []
        if turn_type == "post_primer_bridge" or turn_index == 1:
            return "establishing_wide", "slow_push", paired_ids
        if turn_index % 4 == 0 and len(ordered_ids) > 1:
            partner_index = speaker_index - 1 if speaker_index else 1
            paired_ids = [ordered_ids[partner_index]]
            return "panel_two_shot", "dissolve", paired_ids
        if turn_index % 3 == 0:
            # A close crop of the shared six-seat master removes the desk and
            # body context, leaving the lipsync face floating over the rear
            # screen. Preserve the push-in rhythm with usable medium coverage.
            return "speaker_medium", "slow_push", paired_ids
        return "speaker_medium", "cut", paired_ids

    @staticmethod
    def _apply_seated_panel_turn_shot_plan(
        *,
        primary: Asset,
        turn_index: int,
        camera_view: str,
        camera_action: str,
        scene_asset: Asset,
        wall_screen_asset: Asset | None,
        seating: dict[str, int],
        paired_ids: list[str],
    ) -> None:
        primary.generation_metadata = {
            **primary.generation_metadata,
            "fallback_asset_ids": [],
            "shot_plan": {
                "turn_index": turn_index,
                "primary_asset_id": str(primary.id),
                "studio_panel_scene_asset_id": str(scene_asset.id),
                "wall_screen_broll_asset_id": (
                    str(wall_screen_asset.id) if wall_screen_asset is not None else None
                ),
                "camera_view": camera_view,
                "camera_action": camera_action,
                "camera_transition": "studio_establishing" if turn_index == 1 else camera_action,
                "paired_participant_ids": paired_ids,
                "seating_plan": seating,
                "requires": {
                    "studio_panel_scene": True,
                    "audio_driven_seated_lipsync": True,
                    "wall_screen_broll": wall_screen_asset is not None,
                },
                "speaker_mouth_mode": "audio_driven_seated_panel",
                "wall_screen_media_mode": "rear_display_only",
                "subtitle_style": "speaker_lower_third",
                "citation_overlay_required": False,
            },
        }

    async def generate_visual_assets(
        self,
        episode: Episode,
        request: VisualGenerationRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
        visual_profiles: list[VisualProfile] | None = None,
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        target_assets = self._target_visual_assets(episode, transcript, request)
        if not target_assets:
            raise ValueError("target transcript has no planned visual assets")
        if visual_profiles is not None:
            self._refresh_target_visual_profile_references(
                episode,
                target_assets,
                visual_profiles,
            )

        endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
        workflow_by_id = {workflow.id: workflow for workflow in workflows}
        episode.status = EpisodeStatus.generating_visuals
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.generating_visuals.value},
            )
        )

        completed_count = 0
        failed_count = 0
        fallback_count = 0
        submitted_count = 0
        skipped_count = 0
        for asset in target_assets:
            if asset.status == "completed" and not request.regenerate:
                skipped_count += 1
                continue
            dependency_state = self._visual_dependency_state(episode, asset)
            if dependency_state is not None:
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "generation_blocked_by": dependency_state,
                }
                asset.updated_at = datetime.now(UTC)
                skipped_count += 1
                continue
            if asset.generation_metadata.get("visual_role") == "wall_screen_broll":
                result = await self._materialize_visual_result(
                    None,
                    asset,
                    self._wall_screen_card_visual_result(episode, transcript, asset),
                )
                self._apply_visual_result(asset, result, submitted_by=request.user_id)
                completed_count += 1
                continue
            if self._is_citation_overlay_asset(asset):
                result = await self._materialize_visual_result(
                    None,
                    asset,
                    self._citation_card_visual_result(episode, transcript, asset),
                )
                self._apply_visual_result(asset, result, submitted_by=request.user_id)
                completed_count += 1
                continue
            if request.local_fallback_only:
                result = await self._materialize_visual_result(
                    None,
                    asset,
                    self._fallback_visual_result(
                        episode,
                        transcript,
                        asset,
                        reason="local fallback requested",
                        source_status="local_fallback_only",
                    ),
                )
                self._apply_visual_result(asset, result, submitted_by=request.user_id)
                completed_count += 1
                fallback_count += 1
                continue
            workflow = self._workflow_for_asset(asset, workflow_by_id)
            endpoint = endpoint_by_id.get(workflow.comfyui_endpoint_id)
            if endpoint is None or not endpoint.enabled:
                raise ValueError(
                    f"comfyui endpoint {workflow.comfyui_endpoint_id} is not available"
                )
            if request.regenerate and self._requires_cancel_before_retry(asset):
                cancelled = await self._cancel_existing_job_before_retry(endpoint, asset)
                if not cancelled:
                    failed_count += 1
                    continue
            try:
                result = await self._submit_visual_job(
                    endpoint,
                    workflow,
                    asset,
                    episode=episode,
                    transcript=transcript,
                )
                result = await self._materialize_visual_result(endpoint, asset, result)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                if request.fallback_on_failure and not self._requires_native_directed_visual(asset):
                    result = await self._materialize_visual_result(
                        endpoint,
                        asset,
                        self._fallback_visual_result(
                            episode,
                            transcript,
                            asset,
                            reason=str(exc),
                            source_status="submission_error",
                        ),
                    )
                    self._apply_visual_result(asset, result, submitted_by=request.user_id)
                    completed_count += 1
                    fallback_count += 1
                    continue
                asset.status = "failed"
                asset.storage_uri = None
                asset.mime_type = None
                asset.checksum = None
                generation_attempt_count = int(
                    asset.generation_metadata.get("generation_attempt_count", 0)
                ) + 1
                automatic_retry_available = (
                    generation_attempt_count < self.settings.workflow_stage_retry_max_attempts
                )
                stale_result_metadata = {
                    "adapter",
                    "fallback_visual",
                    "fallback_kind",
                    "fallback_reason",
                    "fallback_source_status",
                    "fallback_provider_metadata",
                    "media_probe",
                    "object_storage_key",
                    "object_storage_path",
                    "object_size_bytes",
                    "storage_backend",
                    "render_ready",
                    "completed_at",
                }
                asset.generation_metadata = {
                    key: value
                    for key, value in asset.generation_metadata.items()
                    if key not in stale_result_metadata
                } | {
                    "adapter": (
                        "b1_managed_media"
                        if self._uses_b1_managed_media_api(endpoint, workflow)
                        else endpoint.adapter_type
                    ),
                    "status": "failed",
                    "failure": str(exc),
                    "failed_at": datetime.now(UTC).isoformat(),
                    "generation_attempt_count": generation_attempt_count,
                    "ready_for_retry": automatic_retry_available,
                    "retry_exhausted": not automatic_retry_available,
                }
                asset.updated_at = datetime.now(UTC)
                failed_count += 1
                continue
            if (
                result.status == "failed"
                and request.fallback_on_failure
                and not self._requires_native_directed_visual(asset)
            ):
                result = await self._materialize_visual_result(
                    endpoint,
                    asset,
                    self._fallback_visual_result(
                        episode,
                        transcript,
                        asset,
                        reason="remote provider returned failed status",
                        source_status=result.status,
                        provider_metadata=result.metadata,
                    ),
                )
                fallback_count += 1

            self._apply_visual_result(asset, result, submitted_by=request.user_id)
            if asset.status == "completed":
                completed_count += 1
            elif asset.status in {"failed", "error"}:
                failed_count += 1
            else:
                submitted_count += 1

        qc = self._visual_generation_qc(episode, transcript, request)
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.assets.generated",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "completed_count": completed_count,
                    "submitted_count": submitted_count,
                    "failed_count": failed_count,
                    "fallback_count": fallback_count,
                    "skipped_count": skipped_count,
                    "visual_qc_status": qc.status,
                    "selection": self._selection_details(request),
                },
            )
        )
        episode.status = EpisodeStatus.ready
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.ready.value},
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    async def repair_directed_visual_assets(
        self,
        episode: Episode,
        request: VisualGenerationRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
        visual_profiles: list[VisualProfile],
    ) -> Episode:
        """Replace only failed/stale directed coverage and preserve completed speaking clips."""
        transcript = self._target_transcript(episode, request)
        seated_panel = self._uses_seated_panel(episode)
        repaired_roles = {
            "studio_seated_character",
            "studio_scene",
            "studio_group_cutaway",
            "reaction_loop",
            "studio_panel_keyframe",
            "video_primary",
            "wall_screen_broll",
        }
        replaced_asset_ids: list[str] = []
        for asset in episode.assets:
            if (
                asset.language == transcript.language
                and asset.status != "replaced"
                and asset.generation_metadata.get("visual_role") in repaired_roles
                and (
                    (
                        seated_panel
                        and asset.generation_metadata.get("visual_role") == "video_primary"
                        and asset.generation_metadata.get("prompt_inputs", {}).get("studio_layout")
                        != "seated_panel"
                    )
                    or
                    asset.status in {"failed", "cancelled", "error"}
                    or asset.generation_metadata.get("render_ready") is False
                    or (
                        asset.status == "completed"
                        and asset.generation_metadata.get("adapter") != "b1_managed_media"
                    )
                )
            ):
                asset.status = "replaced"
                asset.updated_at = datetime.now(UTC)
                replaced_asset_ids.append(str(asset.id))
        episode = self.plan_visual_assets(
            episode,
            VisualAssetPlanRequest(
                transcript_version_id=transcript.id,
                language=transcript.language,
                user_id=request.user_id,
                regenerate=False,
            ),
            visual_profiles=visual_profiles,
            workflows=workflows,
        )
        repair_asset_ids = [
            asset.id
            for asset in episode.assets
            if asset.language == transcript.language
            and asset.status == "planned"
            and asset.generation_metadata.get("visual_role") in repaired_roles
        ]
        if repair_asset_ids:
            episode = await self.generate_visual_assets(
                episode,
                VisualGenerationRequest(
                    transcript_version_id=transcript.id,
                    language=transcript.language,
                    asset_ids=repair_asset_ids,
                    user_id=request.user_id,
                    fallback_on_failure=False,
                ),
                endpoints=endpoints,
                workflows=workflows,
                visual_profiles=visual_profiles,
            )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.assets.directed_repair_planned",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "replaced_asset_ids": replaced_asset_ids,
                    "queued_asset_ids": [str(asset_id) for asset_id in repair_asset_ids],
                },
            )
        )
        return episode

    @staticmethod
    def _requires_native_directed_visual(asset: Asset) -> bool:
        return asset.generation_metadata.get("visual_role") in {
            "studio_seated_character",
            "studio_scene",
            "studio_group_cutaway",
            "reaction_loop",
            "studio_panel_keyframe",
        } or (
            asset.generation_metadata.get("visual_role") == "video_primary"
            and asset.generation_metadata.get("prompt_inputs", {}).get("studio_layout")
            == "seated_panel"
        )

    async def sync_visual_results(
        self,
        episode: Episode,
        request: VisualResultSyncRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        target_assets = self._target_visual_assets(episode, transcript, request)
        sync_assets = [
            asset
            for asset in target_assets
            if asset.status in {"submitted", "running"}
            or (request.include_completed and asset.status == "completed")
        ]
        if not sync_assets:
            raise ValueError("target transcript has no submitted visual jobs to sync")

        endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
        workflow_by_id = {workflow.id: workflow for workflow in workflows}
        completed_count = 0
        failed_count = 0
        fallback_count = 0
        running_count = 0
        skipped_count = 0
        for asset in sync_assets:
            workflow = self._workflow_for_asset(asset, workflow_by_id)
            endpoint = endpoint_by_id.get(workflow.comfyui_endpoint_id)
            job_id = asset.generation_metadata.get("remote_job_id")
            if endpoint is None or endpoint.adapter_type == "mock" or not endpoint.base_url:
                skipped_count += 1
                continue
            if not job_id:
                asset.status = "failed"
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "status": "failed",
                    "failure": "remote job id missing",
                    "failed_at": datetime.now(UTC).isoformat(),
                }
                asset.updated_at = datetime.now(UTC)
                failed_count += 1
                continue
            try:
                result = await self._poll_visual_result(endpoint, asset, str(job_id))
                result = await self._materialize_visual_result(endpoint, asset, result)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                sync_count = int(asset.generation_metadata.get("sync_attempt_count", 0))
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "last_sync_error": str(exc),
                    "sync_attempt_count": sync_count + 1,
                    "last_synced_at": datetime.now(UTC).isoformat(),
                }
                asset.updated_at = datetime.now(UTC)
                running_count += 1
                continue
            if (
                result.status == "failed"
                and request.fallback_on_failure
                and not self._requires_native_directed_visual(asset)
            ):
                result = await self._materialize_visual_result(
                    endpoint,
                    asset,
                    self._fallback_visual_result(
                        episode,
                        transcript,
                        asset,
                        reason="remote provider returned failed status",
                        source_status=result.status,
                        provider_metadata=result.metadata,
                    ),
                )
                fallback_count += 1
            self._apply_visual_result(asset, result, synced_by=request.user_id)
            if asset.status == "completed":
                completed_count += 1
            elif asset.status in {"failed", "error"}:
                failed_count += 1
            else:
                running_count += 1

        qc = self._visual_generation_qc(episode, transcript, request)
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.jobs.synced",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                    "fallback_count": fallback_count,
                    "running_count": running_count,
                    "skipped_count": skipped_count,
                    "visual_qc_status": qc.status,
                    "selection": self._selection_details(request),
                },
            )
        )
        episode.status = EpisodeStatus.ready
        episode.updated_at = datetime.now(UTC)
        return episode

    async def cancel_visual_jobs(
        self,
        episode: Episode,
        request: VisualCancellationRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        target_assets = self._target_visual_assets(episode, transcript, request)
        cancellable_assets = [
            asset for asset in target_assets if asset.status in {"submitted", "running"}
        ]
        if not cancellable_assets:
            raise ValueError("target transcript has no submitted or running visual jobs to cancel")

        endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
        workflow_by_id = {workflow.id: workflow for workflow in workflows}
        cancelled_count = 0
        failed_count = 0
        remote_cancelled_count = 0
        remote_skipped_count = 0
        for asset in cancellable_assets:
            workflow = self._workflow_for_asset(asset, workflow_by_id)
            endpoint = endpoint_by_id.get(workflow.comfyui_endpoint_id)
            job_id = asset.generation_metadata.get("remote_job_id")
            cancel_response: dict | None = None
            remote_cancelled = False
            if (
                endpoint is None
                or endpoint.adapter_type == "mock"
                or not endpoint.base_url
                or not job_id
            ):
                remote_skipped_count += 1
            else:
                try:
                    cancel_response = await self._cancel_visual_job(endpoint, asset, str(job_id))
                    remote_cancelled = True
                    remote_cancelled_count += 1
                except httpx.HTTPError as exc:
                    asset.generation_metadata = {
                        **asset.generation_metadata,
                        "last_cancel_error": str(exc),
                        "last_cancel_attempted_at": datetime.now(UTC).isoformat(),
                        "remote_job_cancellation_required": True,
                    }
                    asset.updated_at = datetime.now(UTC)
                    failed_count += 1
                    continue
            previous_status = asset.status
            asset.status = "planned" if request.reset_to_planned else "cancelled"
            asset.storage_uri = None
            asset.checksum = None
            cancel_count = int(asset.generation_metadata.get("cancellation_attempt_count", 0))
            asset.generation_metadata = {
                **asset.generation_metadata,
                "status": asset.status,
                "previous_status": previous_status,
                "remote_job_id": None,
                "cancelled_remote_job_id": job_id,
                "remote_cancelled": remote_cancelled,
                "remote_cancel_response": self._safe_provider_response_payload(cancel_response),
                "remote_job_cancellation_required": False,
                "cancellation_attempt_count": cancel_count + 1,
                "cancelled_at": datetime.now(UTC).isoformat(),
                "reset_to_planned": request.reset_to_planned,
                "ready_for_retry": request.reset_to_planned,
            }
            asset.updated_at = datetime.now(UTC)
            cancelled_count += 1

        qc = self._visual_generation_qc(episode, transcript, request)
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.jobs.cancelled",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "cancelled_count": cancelled_count,
                    "failed_count": failed_count,
                    "remote_cancelled_count": remote_cancelled_count,
                    "remote_skipped_count": remote_skipped_count,
                    "reset_to_planned": request.reset_to_planned,
                    "selection": self._selection_details(request),
                },
            )
        )
        episode.status = EpisodeStatus.ready
        episode.updated_at = datetime.now(UTC)
        return episode

    def run_visual_quality(
        self,
        episode: Episode,
        request: VisualQualityRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        target_assets = self._target_visual_assets(episode, transcript, request)
        if not target_assets:
            raise ValueError("target transcript has no visual assets to check")

        qc = self._visual_media_qc(
            episode,
            transcript,
            request,
            endpoints=endpoints,
            workflows=workflows,
        )
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="visual.qc.completed",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "checked_count": qc.details["checked_visual_asset_count"],
                    "render_suitable_count": qc.details[
                        "render_suitable_visual_asset_count"
                    ],
                    "lip_sync_ready_count": qc.details[
                        "lip_sync_ready_visual_asset_count"
                    ],
                    "visual_qc_status": qc.status,
                    "selection": self._selection_details(request),
                },
            )
        )
        episode.status = EpisodeStatus.ready
        episode.updated_at = datetime.now(UTC)
        return episode

    def _target_transcript(
        self,
        episode: Episode,
        request: (
            VisualAssetPlanRequest
            | VisualCancellationRequest
            | VisualGenerationRequest
            | VisualQualityRequest
            | VisualResultSyncRequest
        ),
    ) -> TranscriptVersion:
        if request.transcript_version_id is not None:
            for transcript in episode.transcripts:
                if transcript.id == request.transcript_version_id:
                    return self._approved_transcript(transcript)
            raise ValueError("target transcript version not found")
        language = request.language
        if language:
            for transcript in reversed(episode.transcripts):
                if transcript.language == language and transcript.type in {
                    TranscriptType.localized,
                    TranscriptType.broadcast,
                } and transcript.status == "approved":
                    return transcript
            raise ValueError(f"no transcript available for language {language}")
        for transcript in reversed(episode.transcripts):
            if transcript.type == TranscriptType.localized and transcript.status == "approved":
                return transcript
        if episode.canonical_transcript_version_id:
            for transcript in episode.transcripts:
                if transcript.id == episode.canonical_transcript_version_id:
                    return self._approved_transcript(transcript)
        raise ValueError("episode has no target transcript")

    def _approved_transcript(self, transcript: TranscriptVersion) -> TranscriptVersion:
        if transcript.status != "approved":
            raise ValueError("transcript must be approved before visual generation")
        return transcript

    def _visual_reference_images(self, visual_profile: VisualProfile) -> dict:
        references: dict[str, object] = {}
        wardrobe_references = []
        for reference in visual_profile.reference_images:
            payload = reference.model_dump(mode="json")
            references[reference.reference_type] = payload
            if reference.reference_type == "wardrobe":
                wardrobe_references.append(payload)
        if wardrobe_references:
            references["wardrobe_references"] = wardrobe_references
        if visual_profile.reference_image_uri and not references:
            references["portrait"] = {
                "reference_type": "portrait",
                "uri": visual_profile.reference_image_uri,
                "legacy": True,
            }
        return references

    def _visual_reference_download_url(
        self,
        visual_profile: VisualProfile,
        reference_type: str,
    ) -> str:
        return (
            "/api/v1/visual-profiles/"
            f"{quote(visual_profile.id, safe='')}/reference-images/"
            f"{quote(reference_type, safe='')}/download"
        )

    def _visual_reference_download_urls(
        self,
        visual_profile: VisualProfile,
        references: dict,
    ) -> dict:
        download_urls = {}
        for reference_type, reference in references.items():
            if isinstance(reference, dict) and isinstance(reference.get("uri"), str):
                download_urls[reference_type] = self._visual_reference_download_url(
                    visual_profile, reference_type
                )
            if reference_type == "wardrobe_references" and isinstance(reference, list):
                download_urls[reference_type] = [
                    (
                        self._visual_reference_download_url(visual_profile, "wardrobe")
                        + f"?uri={quote(str(item.get('uri')), safe='')}"
                    )
                    for item in reference
                    if isinstance(item, dict) and isinstance(item.get("uri"), str)
                ]
        return download_urls

    def _show_scene_reference_download_url(self, episode: Episode) -> str | None:
        uri = episode.definition.media.scene_reference_image_uri
        if not uri:
            return None
        return (
            "/api/v1/show-media/scene-reference-image/download?uri="
            f"{quote(uri, safe='')}"
        )

    def _visual_reference_uri(
        self,
        visual_profile: VisualProfile,
        *,
        visual_role: str,
        episode: Episode,
    ) -> tuple[str | None, str | None]:
        references = self._visual_reference_images(visual_profile)
        if visual_role == "broll" and episode.definition.media.scene_reference_image_uri:
            return episode.definition.media.scene_reference_image_uri, "show_scene"
        if visual_role == "broll":
            preference = ["full_body", "wardrobe", "portrait"]
        elif visual_role in {"reaction_loop", "video_primary"}:
            preference = ["portrait", "full_body", "wardrobe"]
        else:
            preference = ["full_body", "portrait", "wardrobe"]
        for reference_type in preference:
            reference = references.get(reference_type)
            if isinstance(reference, dict) and isinstance(reference.get("uri"), str):
                return reference["uri"], reference_type
        return None, None

    def _visual_prompt_inputs(
        self,
        episode: Episode,
        transcript_text: str,
        visual_profile: VisualProfile,
        *,
        visual_role: str,
    ) -> dict:
        reference_images = self._visual_reference_images(visual_profile)
        reference_uri, reference_type = self._visual_reference_uri(
            visual_profile,
            visual_role=visual_role,
            episode=episode,
        )
        reference_download_urls = self._visual_reference_download_urls(
            visual_profile,
            reference_images,
        )
        show_scene_reference_download_url = self._show_scene_reference_download_url(episode)
        reference_images_with_downloads = {
            key: {
                **value,
                "download_url": reference_download_urls[key],
            }
            if isinstance(value, dict) and key in reference_download_urls
            else [
                {
                    **item,
                    "download_url": reference_download_urls[key][index],
                }
                if (
                    isinstance(item, dict)
                    and isinstance(reference_download_urls.get(key), list)
                    and index < len(reference_download_urls[key])
                )
                else item
                for index, item in enumerate(value)
            ]
            if isinstance(value, list)
            else value
            for key, value in reference_images.items()
        }
        return {
            "topic": episode.central_question,
            "transcript_text": transcript_text,
            "style_prompt": visual_profile.style_prompt,
            "negative_prompt": visual_profile.negative_prompt,
            "reference_image_uri": reference_uri,
            "reference_image_type": reference_type,
            "reference_image_download_url": (
                show_scene_reference_download_url
                if reference_type == "show_scene"
                else reference_download_urls.get(reference_type)
                if reference_type
                else None
            ),
            "reference_images": reference_images_with_downloads,
            "reference_image_download_urls": reference_download_urls,
            "show_scene_reference_image_uri": episode.definition.media.scene_reference_image_uri,
            "show_scene_reference_image_download_url": show_scene_reference_download_url,
            "portrait_reference_image_uri": reference_images.get("portrait", {}).get("uri"),
            "portrait_reference_image_download_url": reference_download_urls.get("portrait"),
            "full_body_reference_image_uri": reference_images.get("full_body", {}).get("uri"),
            "full_body_reference_image_download_url": reference_download_urls.get("full_body"),
            "wardrobe_reference_image_uri": reference_images.get("wardrobe", {}).get("uri"),
            "wardrobe_reference_image_download_url": reference_download_urls.get("wardrobe"),
            "wardrobe_reference_images": reference_images.get("wardrobe_references", []),
            "wardrobe_reference_image_download_urls": reference_download_urls.get(
                "wardrobe_references", []
            ),
            "seed": visual_profile.seed,
            "performance": visual_profile.performance.model_dump(),
        }

    def _planned_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        turn_id: str,
        asset_type: AssetType,
        visual_role: str,
        shot_type: str,
        visual_profile: VisualProfile,
        workflow: ComfyUiWorkflow,
        transcript_text: str,
        duration_ms: int | None,
        additional_prompt_inputs: dict | None = None,
    ) -> Asset:
        parameters = dict(workflow.default_parameters)
        return Asset(
            episode_id=episode.id,
            asset_type=asset_type,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=turn_id,
            duration_ms=duration_ms,
            width=parameters.get("width"),
            height=parameters.get("height"),
            fps=parameters.get("fps"),
            status="planned",
            generation_metadata={
                "visual_role": visual_role,
                "shot_type": shot_type,
                "transcript_version_id": str(transcript.id),
                "transcript_type": transcript.type.value,
                "localization": transcript.localization_metadata,
                "comfyui_endpoint_id": workflow.comfyui_endpoint_id,
                "comfyui_workflow_id": workflow.id,
                "comfyui_workflow_type": workflow.workflow_type,
                "comfyui_workflow_version": workflow.version,
                "visual_profile_id": visual_profile.id,
                "character_name": visual_profile.character_name,
                "expected_character_name": visual_profile.character_name,
                "expected_style_prompt": visual_profile.style_prompt,
                "prompt_inputs": self._visual_prompt_inputs(
                    episode,
                    transcript_text,
                    visual_profile,
                    visual_role=visual_role,
                )
                | (additional_prompt_inputs or {}),
                "fallback_policy": {
                    "on_generation_failure": [
                        "retry_same_workflow",
                        "switch_reaction_or_studio_shot",
                        "use_citation_card_or_still",
                    ]
                },
            },
        )

    def _planned_reusable_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        source_entity_type: str,
        source_entity_id: str,
        asset_type: AssetType,
        visual_role: str,
        shot_type: str,
        workflow: ComfyUiWorkflow,
        visual_profile: VisualProfile | None,
        prompt_text: str,
        additional_prompt_inputs: dict | None = None,
    ) -> Asset:
        parameters = dict(workflow.default_parameters)
        return Asset(
            episode_id=episode.id,
            asset_type=asset_type,
            language=transcript.language,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            duration_ms=parameters.get("duration_ms", 3000),
            width=parameters.get("width"),
            height=parameters.get("height"),
            fps=parameters.get("fps"),
            status="planned",
            generation_metadata={
                "visual_role": visual_role,
                "shot_type": shot_type,
                "transcript_version_id": str(transcript.id),
                "reusable_visual_asset": True,
                "assigned_participant_id": (
                    source_entity_id if source_entity_type == "participant_profile" else None
                ),
                "comfyui_endpoint_id": workflow.comfyui_endpoint_id,
                "comfyui_workflow_id": workflow.id,
                "comfyui_workflow_type": workflow.workflow_type,
                "comfyui_workflow_version": workflow.version,
                "visual_profile_id": visual_profile.id if visual_profile else None,
                "character_name": visual_profile.character_name if visual_profile else "Studio",
                "expected_character_name": (
                    visual_profile.character_name if visual_profile else "Studio"
                ),
                "expected_style_prompt": (
                    visual_profile.style_prompt
                    if visual_profile
                    else episode.definition.media.visual_style
                ),
                "prompt_inputs": {
                    **(
                    self._visual_prompt_inputs(
                        episode,
                        prompt_text,
                        visual_profile,
                        visual_role=visual_role,
                    )
                    if visual_profile
                    else {
                        "topic": episode.central_question,
                        "transcript_text": prompt_text,
                        "style_prompt": episode.definition.media.visual_style,
                        "negative_prompt": "unreadable text, distorted faces",
                        "reference_image_uri": None,
                        "reference_image_type": None,
                        "reference_images": {},
                        "show_scene_reference_image_uri": (
                            episode.definition.media.scene_reference_image_uri
                        ),
                        "seed": None,
                    }
                    ),
                    **(additional_prompt_inputs or {}),
                },
                "fallback_policy": {
                    "on_generation_failure": [
                        "retry_same_workflow",
                        "use_citation_card_or_still",
                        "mark_manual_review",
                    ]
                },
            },
        )

    @staticmethod
    def _group_reference_image_uris(
        participant_ids: set[str],
        participant_by_id: dict[str, object],
        visual_by_id: dict[str, VisualProfile],
    ) -> list[str]:
        references: list[str] = []
        for participant_id in sorted(participant_ids):
            participant = participant_by_id.get(participant_id)
            visual_profile_id = getattr(participant, "visual_profile_id", None)
            profile = visual_by_id.get(visual_profile_id) if visual_profile_id else None
            if profile is None:
                continue
            full_body = next(
                (
                    reference.uri
                    for reference in profile.reference_images
                    if reference.reference_type == "full_body" and reference.uri
                ),
                None,
            )
            portrait = next(
                (
                    reference.uri
                    for reference in profile.reference_images
                    if reference.reference_type == "portrait" and reference.uri
                ),
                None,
            )
            reference_uri = full_body or portrait or profile.reference_image_uri
            if reference_uri and reference_uri not in references:
                references.append(reference_uri)
        return references

    def _planned_citation_card_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        turn: TranscriptTurn,
        citation_entries: list[dict],
        duration_ms: int | None,
    ) -> Asset:
        source_titles = [
            entry["source_title"]
            for entry in citation_entries
            if isinstance(entry.get("source_title"), str)
        ]
        return Asset(
            episode_id=episode.id,
            asset_type=AssetType.citation_card,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            duration_ms=duration_ms or self._estimate_visual_duration_ms(turn.text),
            width=episode.definition.media.width,
            height=episode.definition.media.height,
            status="planned",
            generation_metadata={
                "visual_role": "citation_overlay",
                "shot_type": "citation_card_overlay",
                "transcript_version_id": str(transcript.id),
                "deterministic_citation_card": True,
                "citation_overlay": True,
                "evidence_refs": sorted(
                    {
                        str(entry["evidence_ref"])
                        for entry in citation_entries
                        if entry.get("evidence_ref")
                    }
                ),
                "citation_entries": citation_entries,
                "source_titles": source_titles,
                "prompt_inputs": {
                    "topic": episode.central_question,
                    "transcript_text": turn.text,
                    "evidence_summary": "; ".join(source_titles),
                },
                "render_ready": False,
                "requires_static_image_duration": True,
            },
        )

    def _latest_evidence_sources_by_id(self, episode: Episode) -> dict[str, dict]:
        evidence_asset = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.evidence_pack
                and asset.status == "completed"
            ),
            None,
        )
        if evidence_asset is None:
            return {}
        pack = evidence_asset.generation_metadata.get("evidence_pack")
        if not isinstance(pack, dict):
            return {}
        return {
            source["id"]: source
            for source in pack.get("source_index", [])
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        }

    def _citation_entries_for_turn(
        self,
        turn: TranscriptTurn,
        evidence_sources_by_id: dict[str, dict],
    ) -> list[dict]:
        entries: list[dict] = []
        for claim in turn.claims:
            for evidence_ref in claim.evidence_refs:
                source = evidence_sources_by_id.get(evidence_ref, {})
                entries.append(
                    {
                        "claim": claim.text,
                        "claim_type": claim.claim_type,
                        "evidence_ref": evidence_ref,
                        "source_title": source.get("title"),
                        "source_uri": source.get("uri"),
                        "source_type": source.get("source_type"),
                        "source_confidence": source.get("confidence"),
                    }
                )
        return entries

    def _transcript_text_for_asset(
        self,
        transcript: TranscriptVersion,
        asset: Asset,
    ) -> str:
        return next(
            (
                turn.text
                for turn in transcript.turns
                if str(turn.id) == asset.source_entity_id
            ),
            "Citation overlay",
        )

    def _apply_turn_shot_plan(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        turn: TranscriptTurn,
        turn_index: int,
    ) -> None:
        primary = self._active_visual_asset(
            episode,
            transcript,
            str(turn.id),
            visual_role="video_primary",
        )
        if primary is None:
            return
        reaction = self._active_reusable_visual_asset(
            episode,
            transcript,
            source_entity_type="participant_profile",
            source_entity_id=turn.speaker_participant_id,
            visual_role="reaction_loop",
        )
        studio = self._active_reusable_visual_asset(
            episode,
            transcript,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            visual_role="studio_scene",
        )
        group_cutaway = self._active_reusable_visual_asset(
            episode,
            transcript,
            source_entity_type="episode",
            source_entity_id=f"{episode.id}:group",
            visual_role="studio_group_cutaway",
        )
        broll = self._active_visual_asset(
            episode,
            transcript,
            str(turn.id),
            visual_role="broll",
        )
        evidence_refs = sorted(
            {
                evidence_ref
                for claim in turn.claims
                for evidence_ref in claim.evidence_refs
            }
        )
        directing = episode.definition.media.directing
        camera_view, camera_action, use_reaction, use_group_cutaway = self._directing_decision(
            turn_index=turn_index,
            directing=directing,
            turn_type=turn.turn_type.value if turn.turn_type is not None else None,
            broll_available=broll is not None,
        )
        fallback_asset_ids = [
            str(asset.id)
            for asset in (reaction, studio, group_cutaway, broll)
            if asset is not None
        ]
        primary.generation_metadata = {
            **primary.generation_metadata,
            "fallback_asset_ids": fallback_asset_ids,
            "shot_plan": {
                "turn_index": turn_index,
                "primary_asset_id": str(primary.id),
                "reusable_reaction_asset_id": (
                    str(reaction.id) if use_reaction and reaction else None
                ),
                "studio_scene_asset_id": str(studio.id) if studio else None,
                "studio_group_cutaway_asset_id": (
                    str(group_cutaway.id) if use_group_cutaway and group_cutaway else None
                ),
                "optional_broll_asset_id": str(broll.id) if broll else None,
                "camera_view": camera_view,
                "camera_action": camera_action,
                "camera_transition": "studio_establishing" if turn_index == 1 else camera_action,
                "requires": {
                    "studio_scene": directing.require_generated_studio,
                    "reaction_loop": use_reaction,
                    "studio_group_cutaway": use_group_cutaway,
                },
                "speaker_mouth_mode": "audio_driven_single_portrait",
                "group_cutaway_audio_mode": "silent_visual_only",
                "subtitle_style": "speaker_lower_third",
                "citation_overlay_required": bool(evidence_refs),
                "evidence_refs": evidence_refs,
            },
        }

    @staticmethod
    def _directing_decision(
        *,
        turn_index: int,
        directing: object,
        turn_type: str | None,
        broll_available: bool,
    ) -> tuple[str, str, bool, bool]:
        mode = getattr(directing, "mode", "studio_directed")
        if mode != "studio_directed":
            return "speaker_medium", "cut", False, False
        if turn_type == "post_primer_bridge" or turn_index == 1:
            return "establishing_wide", "slow_push", False, False
        if broll_available and turn_index % 5 == 0:
            return "contextual_broll", "dissolve", False, False
        if turn_index % 4 == 0:
            return "panel_two_shot", "dissolve", False, True
        if turn_index % 3 == 0:
            return "reaction", "cut", True, False
        if turn_index % 2 == 0:
            return "speaker_close_up", "slow_push", False, False
        return "speaker_medium", "cut", False, False

    def _visual_plan_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        visual_profiles: list[VisualProfile],
        workflows: list[ComfyUiWorkflow],
    ) -> QualityResult:
        seated_panel = self._uses_seated_panel(episode)
        visual_by_id = {profile.id: profile for profile in visual_profiles if profile.enabled}
        workflow_by_id = {workflow.id: workflow for workflow in workflows if workflow.enabled}
        participant_by_id = {participant.id: participant for participant in episode.participants}
        required_turn_ids = {
            str(turn.id) for turn in transcript.turns if turn.status != "excluded"
        }
        required_citation_turn_ids = {
            str(turn.id)
            for turn in transcript.turns
            if turn.status != "excluded"
            and episode.definition.media.generate_citation_cards
            and episode.definition.media.evidence_presentation == "burned_overlays"
            and any(claim.evidence_refs for claim in turn.claims)
        }
        planned_primary_turn_ids = {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.video
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "video_primary"
        }
        planned_citation_turn_ids = {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.citation_card
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "citation_overlay"
        }
        required_reaction_participant_ids = {
            turn.speaker_participant_id
            for turn in transcript.turns
            if turn.status != "excluded"
            and not seated_panel
            and episode.definition.media.directing.mode == "studio_directed"
            and episode.definition.media.directing.require_reaction_cutaways
            and (participant := participant_by_id.get(turn.speaker_participant_id)) is not None
            and participant.visual_profile_id
            and (profile := visual_by_id.get(participant.visual_profile_id)) is not None
            and profile.reaction_workflow_id in workflow_by_id
        }
        planned_reaction_participant_ids = {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.reaction_loop
            and asset.language == transcript.language
            and asset.source_entity_type == "participant_profile"
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "reaction_loop"
        }
        planned_studio_scene_count = sum(
            1
            for asset in episode.assets
            if asset.asset_type == AssetType.studio_scene
            and asset.language == transcript.language
            and asset.source_entity_type == "episode"
            and asset.source_entity_id == str(episode.id)
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "studio_scene"
        )
        planned_studio_group_cutaway_count = sum(
            1
            for asset in episode.assets
            if asset.asset_type == AssetType.studio_scene
            and asset.language == transcript.language
            and asset.source_entity_type == "episode"
            and asset.source_entity_id == f"{episode.id}:group"
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "studio_group_cutaway"
        )
        shot_planned_turn_ids = {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.video
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.status != "replaced"
            and isinstance(asset.generation_metadata.get("shot_plan"), dict)
        }
        issues: list[dict] = []
        for turn in [turn for turn in transcript.turns if turn.status != "excluded"]:
            participant = participant_by_id.get(turn.speaker_participant_id)
            visual_profile = (
                visual_by_id.get(participant.visual_profile_id)
                if participant and participant.visual_profile_id
                else None
            )
            if visual_profile is None:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "missing_visual_profile",
                        "transcript_turn_id": str(turn.id),
                        "participant_id": turn.speaker_participant_id,
                    }
                )
                continue
            if visual_profile.primary_workflow_id not in workflow_by_id:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "missing_primary_visual_workflow",
                        "transcript_turn_id": str(turn.id),
                        "visual_profile_id": visual_profile.id,
                        "workflow_id": visual_profile.primary_workflow_id,
                    }
                )
        missing_primary = sorted(required_turn_ids - planned_primary_turn_ids)
        for turn_id in missing_primary:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "missing_primary_visual_asset",
                    "transcript_turn_id": turn_id,
                }
            )
        missing_reaction_participant_ids = sorted(
            required_reaction_participant_ids - planned_reaction_participant_ids
        )
        for participant_id in missing_reaction_participant_ids:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "missing_reusable_reaction_loop",
                    "participant_id": participant_id,
                }
            )
        directing = episode.definition.media.directing
        if (
            not seated_panel
            and directing.mode == "studio_directed"
            and directing.require_generated_studio
            and self._workflow_by_type(workflow_by_id, "studio_wide_shot")
            and planned_studio_scene_count == 0
        ):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "missing_reusable_studio_scene",
                    "episode_id": str(episode.id),
                }
            )
        if (
            not seated_panel
            and directing.mode == "studio_directed"
            and directing.require_group_cutaways
            and self._workflow_by_type(workflow_by_id, "studio_wide_shot")
            and planned_studio_group_cutaway_count == 0
        ):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "missing_reusable_studio_group_cutaway",
                    "episode_id": str(episode.id),
                }
            )
        missing_shot_plan_turn_ids = sorted(planned_primary_turn_ids - shot_planned_turn_ids)
        for turn_id in missing_shot_plan_turn_ids:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "missing_turn_shot_plan",
                    "transcript_turn_id": turn_id,
                }
            )
        missing_citation_turn_ids = sorted(
            required_citation_turn_ids - planned_citation_turn_ids
        )
        for turn_id in missing_citation_turn_ids:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "missing_citation_overlay_card",
                    "transcript_turn_id": turn_id,
                }
            )
        planned_panel_keyframes = [
            asset
            for asset in episode.assets
            if asset.language == transcript.language
            and asset.asset_type == AssetType.studio_scene
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "studio_panel_keyframe"
        ]
        planned_seated_characters = [
            asset
            for asset in episode.assets
            if asset.language == transcript.language
            and asset.asset_type == AssetType.image
            and asset.status != "replaced"
            and asset.generation_metadata.get("visual_role") == "studio_seated_character"
        ]
        required_seated_participant_ids = {
            turn.speaker_participant_id
            for turn in transcript.turns
            if turn.status != "excluded"
        }
        planned_seated_participant_ids = {
            asset.source_entity_id for asset in planned_seated_characters
        }
        if seated_panel:
            if not episode.definition.media.scene_reference_image_uri:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "missing_seated_panel_studio_reference",
                        "episode_id": str(episode.id),
                    }
                )
            if self._workflow_by_type(workflow_by_id, "studio_seated_character") is None:
                issues.append(
                    {"severity": "fail", "issue": "missing_studio_seated_character_workflow"}
                )
            if self._workflow_by_type(workflow_by_id, "studio_panel_shot") is None:
                issues.append({"severity": "fail", "issue": "missing_studio_panel_workflow"})
            if self._workflow_by_type(workflow_by_id, "seated_panel_lipsync") is None:
                issues.append(
                    {"severity": "fail", "issue": "missing_seated_panel_lipsync_workflow"}
                )
            if not planned_panel_keyframes:
                issues.append({"severity": "fail", "issue": "missing_studio_panel_keyframes"})
            for participant_id in sorted(
                required_seated_participant_ids - planned_seated_participant_ids
            ):
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "missing_studio_seated_character",
                        "participant_id": participant_id,
                    }
                )
        fail_count = sum(1 for issue in issues if issue["severity"] == "fail")
        warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
        if fail_count:
            severity = QualitySeverity.fail
        elif warning_count:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_
        broll_count = sum(
            1
            for asset in episode.assets
            if asset.asset_type == AssetType.broll
            and asset.language == transcript.language
            and asset.status != "replaced"
        )
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="visual_asset_plan_completeness",
            severity=severity,
            status=severity.value,
            score=1.0 if severity == QualitySeverity.pass_ else 0.0,
            details={
                "language": transcript.language,
                "required_visual_turn_count": len(required_turn_ids),
                "planned_primary_visual_asset_count": len(planned_primary_turn_ids),
                "planned_broll_asset_count": broll_count,
                "required_citation_overlay_turn_count": len(required_citation_turn_ids),
                "planned_citation_card_asset_count": len(planned_citation_turn_ids),
                "required_reaction_loop_participant_count": len(
                    required_reaction_participant_ids
                ),
                "planned_reaction_loop_asset_count": len(planned_reaction_participant_ids),
                "planned_studio_scene_asset_count": planned_studio_scene_count,
                "planned_studio_group_cutaway_asset_count": planned_studio_group_cutaway_count,
                "planned_studio_panel_keyframe_count": len(planned_panel_keyframes),
                "planned_studio_seated_character_count": len(planned_seated_characters),
                "seated_panel": seated_panel,
                "shot_planned_turn_count": len(shot_planned_turn_ids),
                "missing_primary_transcript_turn_ids": missing_primary,
                "missing_reaction_loop_participant_ids": missing_reaction_participant_ids,
                "missing_shot_plan_transcript_turn_ids": missing_shot_plan_turn_ids,
                "missing_citation_overlay_transcript_turn_ids": missing_citation_turn_ids,
                "issue_count": len(issues),
                "failure_count": fail_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _target_visual_assets(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        request: (
            VisualCancellationRequest
            | VisualGenerationRequest
            | VisualQualityRequest
            | VisualResultSyncRequest
        ),
    ) -> list[Asset]:
        requested_asset_ids = (
            {str(asset_id) for asset_id in request.asset_ids}
            if request.asset_ids is not None
            else None
        )
        requested_turn_ids = (
            {str(turn_id) for turn_id in request.transcript_turn_ids}
            if request.transcript_turn_ids is not None
            else None
        )
        requested_participant_ids = set(request.participant_ids or [])
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        turn_ids = {str(turn.id) for turn in playable_turns}
        participant_by_turn_id = {
            str(turn.id): turn.speaker_participant_id for turn in playable_turns
        }
        return [
            asset
            for asset in episode.assets
            if self._visual_asset_matches_request(
                asset,
                episode,
                transcript,
                turn_ids=turn_ids,
                participant_by_turn_id=participant_by_turn_id,
                requested_asset_ids=requested_asset_ids,
                requested_turn_ids=requested_turn_ids,
                requested_participant_ids=requested_participant_ids,
                failed_only=request.failed_only,
            )
        ]

    def _visual_asset_matches_request(
        self,
        asset: Asset,
        episode: Episode,
        transcript: TranscriptVersion,
        turn_ids: set[str],
        participant_by_turn_id: dict[str, str],
        requested_asset_ids: set[str] | None,
        requested_turn_ids: set[str] | None,
        requested_participant_ids: set[str],
        failed_only: bool,
    ) -> bool:
        if asset.asset_type not in {
            AssetType.video,
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
            AssetType.citation_card,
            AssetType.image,
        }:
            return False
        if asset.language != transcript.language:
            return False
        if asset.status not in {
            "planned",
            "submitted",
            "running",
            "failed",
            "cancelled",
            "completed",
        }:
            return False
        if requested_asset_ids is not None and str(asset.id) not in requested_asset_ids:
            return False
        if failed_only and asset.status != "failed":
            return False
        if asset.source_entity_type == "transcript_turn":
            if asset.source_entity_id not in turn_ids:
                return False
            if requested_turn_ids is not None and asset.source_entity_id not in requested_turn_ids:
                return False
            return (
                not requested_participant_ids
                or participant_by_turn_id.get(asset.source_entity_id) in requested_participant_ids
            )
        if requested_turn_ids is not None:
            return False
        if asset.source_entity_type == "participant_profile":
            return (
                not requested_participant_ids
                or asset.source_entity_id in requested_participant_ids
            )
        if asset.source_entity_type == "episode":
            is_episode_reusable_asset = asset.source_entity_id == str(episode.id)
            is_group_cutaway = (
                asset.generation_metadata.get("visual_role") == "studio_group_cutaway"
                and asset.source_entity_id == f"{episode.id}:group"
            )
            is_panel_keyframe = (
                asset.generation_metadata.get("visual_role") == "studio_panel_keyframe"
                and asset.source_entity_id.startswith(f"{episode.id}:panel:")
            )
            return (
                is_episode_reusable_asset or is_group_cutaway or is_panel_keyframe
            ) and not requested_participant_ids
        return False

    @staticmethod
    def _visual_dependency_state(episode: Episode, asset: Asset) -> dict | None:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        dependency_ids = (
            prompt_inputs.get("depends_on_asset_ids")
            if isinstance(prompt_inputs, dict)
            else None
        )
        if not isinstance(dependency_ids, list) or not dependency_ids:
            return None
        assets_by_id = {str(candidate.id): candidate for candidate in episode.assets}
        pending: list[str] = []
        failed: list[str] = []
        panel_review_required: list[str] = []
        panel_rejected: list[str] = []
        seated_review_required: list[str] = []
        seated_rejected: list[str] = []
        for asset_id in dependency_ids:
            dependency = assets_by_id.get(str(asset_id))
            if dependency is None or dependency.status == "replaced":
                failed.append(str(asset_id))
            elif dependency.generation_metadata.get("visual_role") == "studio_panel_keyframe":
                approval_status = str(
                    dependency.generation_metadata.get("approval_status") or "pending_review"
                )
                if approval_status == "rejected":
                    panel_rejected.append(str(asset_id))
                elif approval_status != "approved":
                    panel_review_required.append(str(asset_id))
                elif (
                    dependency.status != "completed"
                    or dependency.generation_metadata.get("render_ready") is False
                ):
                    if dependency.status in {"failed", "cancelled", "error"}:
                        failed.append(str(asset_id))
                    else:
                        pending.append(str(asset_id))
            elif dependency.generation_metadata.get("visual_role") == "studio_seated_character":
                approval_status = str(
                    dependency.generation_metadata.get("approval_status") or "pending_review"
                )
                if dependency.status in {"failed", "cancelled", "error"}:
                    failed.append(str(asset_id))
                elif dependency.status != "completed":
                    pending.append(str(asset_id))
                elif approval_status == "rejected":
                    seated_rejected.append(str(asset_id))
                elif approval_status != "approved":
                    seated_review_required.append(str(asset_id))
                elif dependency.generation_metadata.get("render_ready") is False:
                    failed.append(str(asset_id))
            elif (
                dependency.status != "completed"
                or dependency.generation_metadata.get("render_ready") is False
            ):
                if dependency.status in {"failed", "cancelled", "error"}:
                    failed.append(str(asset_id))
                else:
                    pending.append(str(asset_id))
        if not any(
            (
                pending,
                failed,
                panel_review_required,
                panel_rejected,
                seated_review_required,
                seated_rejected,
            )
        ):
            return None
        if seated_rejected:
            reason = "seated_character_review_rejected"
        elif seated_review_required:
            reason = "seated_character_review_required"
        elif panel_rejected:
            reason = "studio_panel_review_rejected"
        elif panel_review_required:
            reason = "studio_panel_review_required"
        else:
            reason = "dependency_failed" if failed else "dependency_pending"
        return {
            "reason": reason,
            "pending_asset_ids": pending,
            "failed_asset_ids": failed,
            "review_required_asset_ids": panel_review_required + seated_review_required,
            "rejected_asset_ids": panel_rejected + seated_rejected,
        }

    async def _submit_visual_job(
        self,
        endpoint: ComfyUiEndpoint,
        workflow: ComfyUiWorkflow,
        asset: Asset,
        *,
        episode: Episode | None = None,
        transcript: TranscriptVersion | None = None,
    ) -> VisualResult:
        if endpoint.adapter_type == "mock":
            return self._mock_visual_result(endpoint, workflow, asset)
        if not endpoint.base_url:
            raise ValueError("comfyui endpoint requires base_url")
        if self._uses_b1_managed_media_api(endpoint, workflow):
            return await self._submit_b1_managed_media_job(
                endpoint,
                workflow,
                asset,
                episode=episode,
                transcript=transcript,
            )

        headers = {"accept": "application/json", "content-type": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        patched_prompt, prompt_context, patch_bindings = self._patched_api_workflow(
            workflow,
            asset,
        )
        payload = {
            "client_id": f"dialecticore-{asset.id}",
            "prompt": patched_prompt,
            "extra_data": {
                "asset_id": str(asset.id),
                "source_entity_type": asset.source_entity_type,
                "source_entity_id": asset.source_entity_id,
                "visual_role": asset.generation_metadata.get("visual_role"),
                "shot_type": asset.generation_metadata.get("shot_type"),
                "prompt_inputs": asset.generation_metadata.get("prompt_inputs", {}),
                "workflow": {
                    "id": workflow.id,
                    "type": workflow.workflow_type,
                    "version": workflow.version,
                },
                "output": {
                    "asset_type": asset.asset_type.value,
                    "width": asset.width,
                    "height": asset.height,
                    "fps": asset.fps,
                },
            },
        }
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.post(f"{endpoint.base_url.rstrip('/')}/prompt", json=payload)
            response.raise_for_status()
            data = response.json()
        result = self._visual_result_from_payload(
            endpoint=endpoint,
            asset=asset,
            payload=data,
            default_job_id=data.get("prompt_id") or data.get("job_id"),
            default_status="submitted",
        )
        return replace(
            result,
            metadata={
                **result.metadata,
                "resolved_prompt_inputs": prompt_context,
                "workflow_patch_bindings": patch_bindings,
            },
        )

    async def _submit_b1_managed_media_job(
        self,
        endpoint: ComfyUiEndpoint,
        workflow: ComfyUiWorkflow,
        asset: Asset,
        *,
        episode: Episode | None = None,
        transcript: TranscriptVersion | None = None,
    ) -> VisualResult:
        headers = {"accept": "application/json", "content-type": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        prompt_context = self._managed_media_prompt_context(workflow, asset)
        api_base = str(
            workflow.default_parameters.get("b1_managed_api_base")
            or endpoint.capabilities.get("remote_nodes_api_base")
            or endpoint.base_url
        ).rstrip("/")
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            if workflow.workflow_type == "studio_seated_character":
                if episode is None or transcript is None:
                    raise ValueError(
                        "studio-seated-character requires episode and transcript context"
                    )
                payload, upload_metadata = await self._b1_seated_character_payload(
                    client=client,
                    api_base=api_base,
                    workflow=workflow,
                    episode=episode,
                    asset=asset,
                )
            elif workflow.workflow_type == "studio_panel_shot":
                if episode is None or transcript is None:
                    raise ValueError("studio-panel-shot requires episode and transcript context")
                payload, upload_metadata = await self._b1_studio_panel_payload(
                    workflow=workflow,
                    episode=episode,
                    transcript=transcript,
                    asset=asset,
                )
            elif self._is_b1_audio_driven_lipsync_workflow(workflow):
                if episode is None or transcript is None:
                    raise ValueError("talking-head lipsync requires episode and transcript context")
                payload, upload_metadata = await self._b1_lipsync_payload(
                    client=client,
                    api_base=api_base,
                    workflow=workflow,
                    episode=episode,
                    transcript=transcript,
                    asset=asset,
                )
            else:
                payload, upload_metadata = await self._b1_managed_media_payload(
                    client=client,
                    api_base=api_base,
                    workflow=workflow,
                    asset=asset,
                    context=prompt_context,
            )
            response = await client.post(
                f"{api_base}/v1/media/jobs",
                json=payload,
                headers={
                    **headers,
                    # Reusing an asset id after its references changed makes B1
                    # correctly reject the request as an idempotency conflict.
                    # Keep retries within an attempt idempotent while giving each
                    # deliberate generation attempt a distinct request identity.
                    "Idempotency-Key": (
                        f"dialecticore-visual-{asset.id}-attempt-"
                        f"{int(asset.generation_metadata.get('generation_attempt_count', 0)) + 1}"
                    ),
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    error_payload = self._safe_provider_response_payload(response.json())
                except ValueError:
                    error_payload = response.text[:500]
                raise ValueError(
                    f"B1 managed media job request failed with HTTP {response.status_code}: "
                    f"{error_payload}"
                ) from exc
            data = response.json()
        job_id = data.get("id") or data.get("job_id") or data.get("b1_job_id")
        if not job_id:
            raise ValueError("B1 managed media job response did not include a job id")
        result = self._visual_result_from_payload(
            endpoint=endpoint,
            asset=asset,
            payload=data,
            default_job_id=job_id,
            default_status="submitted",
        )
        return replace(
            result,
            status=(
                "submitted"
                if result.status == "completed" and not result.storage_uri
                else result.status
            ),
            metadata={
                **result.metadata,
                "adapter": "b1_managed_media",
                "managed_media_api_base": api_base,
                "managed_media_payload": self._safe_provider_response_payload(payload),
                "managed_media_prompt_context": prompt_context,
                "remote_job_id": job_id,
                **upload_metadata,
            },
        )

    def _uses_b1_managed_media_api(
        self,
        endpoint: ComfyUiEndpoint,
        workflow: ComfyUiWorkflow,
    ) -> bool:
        if not workflow.default_parameters.get("b1_media_preset"):
            return False
        if not (
            workflow.default_parameters.get("managed_b1_media_api")
            or workflow.default_parameters.get("b1_managed_api_base")
        ):
            return False
        endpoint_api_base = str(endpoint.capabilities.get("remote_nodes_api_base") or "").strip()
        if endpoint_api_base:
            return True
        endpoint_host = urlparse(str(endpoint.base_url or "")).hostname or ""
        managed_api_host = urlparse(
            str(workflow.default_parameters.get("b1_managed_api_base") or "")
        ).hostname
        return endpoint_host.endswith(".b1.germering") and managed_api_host == "api.ai.b1.germering"

    def _is_b1_audio_driven_lipsync_workflow(self, workflow: ComfyUiWorkflow) -> bool:
        # Existing installations may still persist the former `video-image` preset.
        # A B1-managed talking-head workflow is always audio-driven and must not fall
        # back to the generic Wan image-animation operation.
        return workflow.workflow_type in {"talking_head", "seated_panel_lipsync"}

    async def _b1_seated_character_payload(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        workflow: ComfyUiWorkflow,
        episode: Episode,
        asset: Asset,
    ) -> tuple[dict, dict]:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        values = prompt_inputs if isinstance(prompt_inputs, dict) else {}
        participant_id = str(values.get("participant_id") or asset.source_entity_id or "")
        seat = values.get("seat")
        portrait_uri = values.get("portrait_reference_image_uri")
        full_body_uri = values.get("full_body_reference_image_uri")
        studio_uri = values.get("show_scene_reference_image_uri")
        if not participant_id:
            raise ValueError("studio-seated-character requires a participant id")
        if not isinstance(seat, int) or isinstance(seat, bool) or seat < 1:
            raise ValueError(
                f"studio-seated-character requires a valid seat for {participant_id}"
            )
        for label, uri in (
            ("portrait", portrait_uri),
            ("full-body", full_body_uri),
            ("studio", studio_uri),
        ):
            if not isinstance(uri, str) or not uri:
                raise ValueError(
                    f"studio-seated-character requires a {label} reference for "
                    f"{participant_id}"
                )

        portrait_source = self._private_object_bytes(
            portrait_uri, f"seated character portrait for {participant_id}"
        )
        full_body_source = self._private_object_bytes(
            full_body_uri, f"seated character full-body reference for {participant_id}"
        )
        studio_source = self._private_object_bytes(studio_uri, "seated character studio reference")
        portrait_payload, portrait_content_type, portrait_repacked = (
            self._prepare_b1_image_for_upload(
                portrait_source, self._image_content_type_for_uri(portrait_uri)
            )
        )
        full_body_payload, full_body_content_type, full_body_repacked = (
            self._prepare_b1_image_for_upload(
                full_body_source, self._image_content_type_for_uri(full_body_uri)
            )
        )
        studio_payload, studio_content_type, studio_repacked = (
            self._prepare_b1_image_for_upload(
                studio_source, self._image_content_type_for_uri(studio_uri)
            )
        )
        studio_sha256 = hashlib.sha256(studio_source).hexdigest()
        studio_reference = self._reusable_b1_studio_upload_reference(
            episode, studio_sha256
        )
        if studio_reference is None:
            studio_reference = await self._upload_b1_private_media(
                client,
                api_base,
                payload=studio_payload,
                content_type=studio_content_type,
                field="studio_reference",
            )
        portrait_reference = await self._upload_b1_private_media(
            client,
            api_base,
            payload=portrait_payload,
            content_type=portrait_content_type,
            field="portrait",
        )
        full_body_reference = await self._upload_b1_private_media(
            client,
            api_base,
            payload=full_body_payload,
            content_type=full_body_content_type,
            field="full_body",
        )
        seed = values.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            attempt = int(asset.generation_metadata.get("generation_attempt_count", 0)) + 1
            seed_material = (
                f"{episode.id}:{participant_id}:{seat}:seated-character:{attempt}".encode()
            )
            seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
        payload = {
            "modality": "image",
            "operation": "studio-seated-character",
            "model": str(
                workflow.default_parameters.get("b1_media_preset")
                or "studio-seated-character-p40"
            ),
            "input": {
                "participant_id": participant_id,
                "portrait_artifact_id": portrait_reference,
                "full_body_artifact_id": full_body_reference,
                "studio_reference_artifact_id": studio_reference,
                "seat": seat,
                "pose": "neutral_seated",
                "camera_view": "establishing_wide",
                "camera_angle": "front_three_quarter",
                "width": int(
                    workflow.default_parameters.get("b1_media_width") or 1280
                ),
                "height": int(
                    workflow.default_parameters.get("b1_media_height") or 720
                ),
                "seed": abs(seed) % 2_147_483_648,
            },
            "priority": str(
                workflow.default_parameters.get("b1_media_priority") or "single_image"
            ),
            "runtime_policy": str(
                workflow.default_parameters.get("b1_media_runtime_policy") or "any"
            ),
        }
        return payload, {
            "seated_character": {
                "participant_id": participant_id,
                "seat": seat,
                "pose": "neutral_seated",
                "camera_view": "establishing_wide",
                "camera_angle": "front_three_quarter",
            },
            "b1_upload_references": {
                "studio_reference": {
                    "id": studio_reference,
                    "sha256": studio_sha256,
                    "repacked_for_b1": studio_repacked,
                    "content_type": studio_content_type,
                },
                participant_id: {
                    "portrait": portrait_reference,
                    "full_body": full_body_reference,
                    "portrait_sha256": hashlib.sha256(portrait_source).hexdigest(),
                    "full_body_sha256": hashlib.sha256(full_body_source).hexdigest(),
                    "portrait_repacked_for_b1": portrait_repacked,
                    "full_body_repacked_for_b1": full_body_repacked,
                    "portrait_content_type": portrait_content_type,
                    "full_body_content_type": full_body_content_type,
                },
            },
        }

    @staticmethod
    def _reusable_b1_studio_upload_reference(
        episode: Episode,
        studio_sha256: str,
    ) -> str | None:
        for candidate in reversed(episode.assets):
            if candidate.status == "replaced":
                continue
            references = candidate.generation_metadata.get("b1_upload_references")
            if not isinstance(references, dict):
                continue
            studio_reference = references.get("studio_reference")
            if not isinstance(studio_reference, dict):
                continue
            reference_id = studio_reference.get("id")
            if (
                studio_reference.get("sha256") == studio_sha256
                and isinstance(reference_id, str)
                and reference_id
            ):
                return reference_id
        return None

    async def _b1_studio_panel_payload(
        self,
        *,
        workflow: ComfyUiWorkflow,
        episode: Episode,
        transcript: TranscriptVersion,
        asset: Asset,
    ) -> tuple[dict, dict]:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        values = prompt_inputs if isinstance(prompt_inputs, dict) else {}
        studio_uri = values.get("show_scene_reference_image_uri")
        if not isinstance(studio_uri, str) or not studio_uri:
            raise ValueError("studio-panel-shot requires an uploaded studio reference image")
        raw_participants = values.get("panel_participants")
        if not isinstance(raw_participants, list) or not raw_participants:
            raise ValueError("studio-panel-shot requires assigned participant references")
        assets_by_id = {str(candidate.id): candidate for candidate in episode.assets}
        participants: list[dict] = []
        upload_references: dict[str, dict] = {}
        studio_reference: str | None = None
        studio_sha256: str | None = None
        for raw in raw_participants:
            if not isinstance(raw, dict):
                continue
            participant_id = raw.get("participant_id")
            seat = raw.get("seat")
            seated_asset_id = raw.get("seated_character_asset_id")
            if not isinstance(participant_id, str) or not participant_id:
                raise ValueError("studio-panel-shot participant id is missing")
            if not isinstance(seat, int) or seat < 1:
                raise ValueError(f"studio-panel-shot requires a valid seat for {participant_id}")
            seated_asset = assets_by_id.get(str(seated_asset_id or ""))
            if (
                seated_asset is None
                or seated_asset.generation_metadata.get("visual_role")
                != "studio_seated_character"
                or seated_asset.source_entity_id != participant_id
            ):
                raise ValueError(
                    f"studio-panel-shot requires the planned seated plate for {participant_id}"
                )
            if seated_asset.status != "completed":
                raise ValueError(
                    f"studio-panel-shot is waiting for the seated plate for {participant_id}"
                )
            if seated_asset.generation_metadata.get("approval_status") != "approved":
                raise ValueError(
                    f"studio-panel-shot requires an approved seated plate for {participant_id}"
                )
            seated_reference = self._seated_reference_artifact_id(seated_asset)
            if not seated_reference:
                raise ValueError(
                    f"approved seated plate for {participant_id} is missing its B1 reference"
                )
            references = seated_asset.generation_metadata.get("b1_upload_references")
            if not isinstance(references, dict):
                raise ValueError(
                    f"seated plate for {participant_id} is missing its B1 input provenance"
                )
            participant_references = references.get(participant_id)
            plate_studio = references.get("studio_reference")
            if not isinstance(participant_references, dict) or not isinstance(
                plate_studio, dict
            ):
                raise ValueError(
                    f"seated plate for {participant_id} has incomplete B1 input provenance"
                )
            portrait_reference = str(participant_references.get("portrait") or "").strip()
            full_body_reference = str(participant_references.get("full_body") or "").strip()
            candidate_studio_reference = str(plate_studio.get("id") or "").strip()
            candidate_studio_sha256 = str(plate_studio.get("sha256") or "").strip()
            if not portrait_reference or not full_body_reference or not candidate_studio_reference:
                raise ValueError(
                    f"seated plate for {participant_id} has incomplete reusable B1 references"
                )
            if studio_reference is None:
                studio_reference = candidate_studio_reference
                studio_sha256 = candidate_studio_sha256 or None
            elif (
                candidate_studio_reference != studio_reference
                or (studio_sha256 and candidate_studio_sha256 != studio_sha256)
            ):
                raise ValueError(
                    "studio-panel-shot requires all seated plates to use the same studio upload"
                )
            participants.append(
                {
                    "participant_id": participant_id,
                    "seat": seat,
                    "portrait_artifact_id": portrait_reference,
                    "full_body_artifact_id": full_body_reference,
                    "seated_reference_artifact_id": seated_reference,
                }
            )
            upload_references[participant_id] = {
                **participant_references,
                "seated_reference": seated_reference,
                "seated_character_asset_id": str(seated_asset.id),
            }
        if not studio_reference:
            raise ValueError("studio-panel-shot has no reusable B1 studio reference")
        requested_camera_view = str(values.get("camera_view") or "speaker_medium")
        camera_view = "establishing_wide"
        participant_ids = {
            str(participant.get("participant_id") or "") for participant in participants
        }
        stature_reference_participant_id = str(
            values.get("stature_reference_participant_id") or ""
        ).strip()
        if not stature_reference_participant_id:
            stature_reference_participant_id = (
                "claude"
                if "claude" in participant_ids
                else str(participants[0]["participant_id"])
            )
        if stature_reference_participant_id not in participant_ids:
            raise ValueError(
                "studio-panel-shot stature reference must name a panel participant"
            )
        panel_input = {
            "studio_reference_artifact_id": studio_reference,
            "participants": participants,
            "stature_reference_participant_id": stature_reference_participant_id,
            "camera": {
                "view": camera_view,
                "action": "cut",
            },
            "width": int(workflow.default_parameters.get("b1_media_width") or 512),
            "height": int(workflow.default_parameters.get("b1_media_height") or 288),
            "wall_screen_mode": "available",
        }
        seed = values.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            seed_material = f"{episode.id}:{asset.id}:studio-panel".encode()
            seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
        panel_input["seed"] = abs(seed) % 2_147_483_648
        payload = {
            "modality": "image",
            "operation": "studio-panel-shot",
            "model": "studio-panel-shot",
            "input": panel_input,
            "priority": str(
                workflow.default_parameters.get("b1_media_priority") or "single_image"
            ),
            "runtime_policy": str(
                workflow.default_parameters.get("b1_media_runtime_policy") or "comfyui"
            ),
        }
        return payload, {
            "studio_panel": {
                "coverage_key": values.get("panel_coverage_key"),
                "camera_view": camera_view,
                "requested_camera_view": requested_camera_view,
                "camera_action": "cut",
                "camera_source": "b1_master_panel_plate",
                "stature_reference_participant_id": stature_reference_participant_id,
                "seating_plan": values.get("seating_plan"),
            },
            "b1_upload_references": {
                **upload_references,
                "studio_reference": {
                    "id": studio_reference,
                    "sha256": studio_sha256,
                },
            },
        }

    @staticmethod
    def _seated_reference_artifact_id(asset: Asset) -> str | None:
        """Resolve B1's reusable seated-plate reference across compatible responses."""
        candidates: list[object] = []
        for container_key in ("seated_character", "b1_approval_response", "provider_response"):
            container = asset.generation_metadata.get(container_key)
            if not isinstance(container, dict):
                continue
            candidates.extend(
                container.get(key)
                for key in (
                    "seated_reference_artifact_id",
                    "reference_id",
                    "artifact_id",
                    "reference",
                )
            )
            nested = container.get("seated_character")
            if isinstance(nested, dict):
                candidates.extend(
                    nested.get(key)
                    for key in (
                        "seated_reference_artifact_id",
                        "reference_id",
                        "artifact_id",
                        "reference",
                    )
                )
        provider_response = asset.generation_metadata.get("provider_response")
        artifacts = (
            provider_response.get("artifacts")
            if isinstance(provider_response, dict)
            else []
        )
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    candidates.extend(
                        artifact.get(key)
                        for key in ("reference", "reference_id", "artifact_id", "id")
                    )
        return next(
            (
                str(candidate).strip()
                for candidate in candidates
                if isinstance(candidate, str) and str(candidate).strip().startswith("upload_")
            ),
            None,
        )

    async def _b1_lipsync_payload(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        workflow: ComfyUiWorkflow,
        episode: Episode,
        transcript: TranscriptVersion,
        asset: Asset,
    ) -> tuple[dict, dict]:
        portrait_uri = self._b1_lipsync_portrait_uri(asset)
        portrait_payload = self._private_object_bytes(
            portrait_uri,
            "talking-head lipsync portrait reference",
        )
        portrait_content_type = self._image_content_type_for_uri(portrait_uri)
        b1_portrait_payload, b1_portrait_content_type, portrait_repacked_for_b1 = (
            self._prepare_b1_image_for_upload(portrait_payload, portrait_content_type)
        )
        audio_asset = self._audio_asset_for_visual(episode, transcript, asset)
        if audio_asset is None or not audio_asset.storage_uri:
            raise ValueError("talking-head lipsync requires a completed WAV dialogue asset")
        if (audio_asset.mime_type or "").split(";", 1)[0].strip().lower() not in {
            "audio/wav",
            "audio/x-wav",
        }:
            raise ValueError("talking-head lipsync dialogue asset must be WAV")
        audio_payload = self._private_object_bytes(
            audio_asset.storage_uri,
            "talking-head lipsync dialogue WAV",
        )
        source_audio_sha256 = hashlib.sha256(audio_payload).hexdigest()
        upload_audio_payload = self._finalize_streamed_wav_for_b1(audio_payload)
        audio_duration_ms = self._wav_duration_ms(upload_audio_payload)
        portrait_reference = await self._upload_b1_private_media(
            client,
            api_base,
            payload=b1_portrait_payload,
            content_type=b1_portrait_content_type,
            field="portrait",
        )
        audio_reference = await self._upload_b1_private_media(
            client,
            api_base,
            payload=upload_audio_payload,
            content_type="audio/wav",
            field="audio",
        )
        width = int(workflow.default_parameters.get("b1_lipsync_width") or 512)
        height = int(workflow.default_parameters.get("b1_lipsync_height") or 512)
        fps = int(workflow.default_parameters.get("b1_lipsync_fps") or 12)
        if workflow.workflow_type == "seated_panel_lipsync" and (width < 1024 or height < 576):
            raise ValueError(
                "B1 native seated-panel camera coverage requires the workflow output "
                "to be at least 1024x576"
            )
        audio_sha256 = hashlib.sha256(upload_audio_payload).hexdigest()
        performance_plan = self._b1_character_performance_plan(asset)
        input_payload = {
            "portrait_artifact_id": portrait_reference,
            "audio_artifact_id": audio_reference,
            "audio_sha256": audio_sha256,
            "width": width,
            "height": height,
            "fps": fps,
            "duration_ms": audio_duration_ms,
            "performance_plan": performance_plan,
        }
        additional_metadata: dict = {}
        if workflow.workflow_type == "seated_panel_lipsync":
            seated_payload, seated_metadata = await self._b1_seated_panel_lipsync_inputs(
                client=client,
                api_base=api_base,
                episode=episode,
                asset=asset,
            )
            input_payload.update(seated_payload)
            additional_metadata.update(seated_metadata)
        payload = {
            "modality": "video",
            "operation": "talking-head-lipsync",
            "model": "talking-head-lipsync",
            "input": input_payload,
            "priority": str(
                workflow.default_parameters.get("b1_media_priority") or "single_video"
            ),
            "runtime_policy": str(
                workflow.default_parameters.get("b1_media_runtime_policy") or "comfyui"
            ),
        }
        return payload, {
            "lip_sync_mode": "audio_driven",
            "portrait_reference_uri": portrait_uri,
            "portrait_sha256": hashlib.sha256(portrait_payload).hexdigest(),
            "b1_portrait_sha256": hashlib.sha256(b1_portrait_payload).hexdigest(),
            "portrait_repacked_for_b1": portrait_repacked_for_b1,
            "b1_portrait_content_type": b1_portrait_content_type,
            "audio_asset_id": str(audio_asset.id),
            "audio_sha256": audio_sha256,
            "source_audio_sha256": source_audio_sha256,
            "audio_duration_ms": audio_duration_ms,
            "audio_repacked_for_b1": upload_audio_payload != audio_payload,
            "performance_plan": performance_plan,
            "b1_upload_references": {
                "portrait": portrait_reference,
                "audio": audio_reference,
            },
            **additional_metadata,
        }

    async def _b1_seated_panel_lipsync_inputs(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        episode: Episode,
        asset: Asset,
    ) -> tuple[dict, dict]:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        values = prompt_inputs if isinstance(prompt_inputs, dict) else {}
        scene_asset_id = values.get("scene_keyframe_asset_id")
        if not isinstance(scene_asset_id, str) or not scene_asset_id:
            raise ValueError("seated panel lipsync requires a studio panel keyframe asset")
        scene_asset = next(
            (candidate for candidate in episode.assets if str(candidate.id) == scene_asset_id),
            None,
        )
        if (
            scene_asset is None
            or scene_asset.status != "completed"
            or not scene_asset.storage_uri
            or scene_asset.generation_metadata.get("render_ready") is False
        ):
            raise ValueError(
                "seated panel lipsync is waiting for its completed studio panel keyframe"
            )
        studio_panel = scene_asset.generation_metadata.get("studio_panel")
        if not isinstance(studio_panel, dict):
            provider_response = scene_asset.generation_metadata.get("provider_response")
            studio_panel = (
                provider_response.get("studio_panel")
                if isinstance(provider_response, dict)
                and isinstance(provider_response.get("studio_panel"), dict)
                else {}
            )
        scene_reference = studio_panel.get("scene_artifact_id")
        scene_source = "b1_studio_panel_result"
        scene_payload: bytes | None = None
        if not isinstance(scene_reference, str) or not scene_reference:
            scene_payload = self._private_object_bytes(
                scene_asset.storage_uri,
                "seated panel keyframe",
            )
            scene_reference = await self._upload_b1_private_media(
                client,
                api_base,
                payload=scene_payload,
                content_type=scene_asset.mime_type or "image/png",
                field="scene",
            )
            scene_source = "uploaded_keyframe_fallback"
        speaker_id = values.get("speaker_participant_id")
        if not isinstance(speaker_id, str) or not speaker_id:
            raise ValueError("seated panel lipsync requires its selected speaker")
        requested_camera_view = str(values.get("camera_view") or "speaker_medium")
        b1_camera_view = {
            "speaker_close_up": "speaker_close",
        }.get(requested_camera_view, requested_camera_view)
        supported_camera_views = {
            "establishing_wide",
            "speaker_medium",
            "speaker_close",
            "panel_two_shot",
            "reaction",
        }
        if b1_camera_view not in supported_camera_views:
            raise ValueError(
                f"B1 seated-panel camera view is unsupported: {requested_camera_view}"
            )
        if b1_camera_view != "establishing_wide" and (
            (scene_asset.width or 0) < 1024 or (scene_asset.height or 0) < 576
        ):
            raise ValueError(
                "B1 native seated-panel camera coverage requires a completed "
                "1024x576 or larger studio master"
            )
        paired_ids = values.get("paired_participant_ids")
        framed_participant_ids = [speaker_id]
        if isinstance(paired_ids, list):
            framed_participant_ids.extend(
                participant_id
                for participant_id in paired_ids
                if isinstance(participant_id, str)
                and participant_id
                and participant_id != speaker_id
            )
        # Keep an ordered, deduplicated payload. B1 uses this sequence for
        # two-shots and reaction framing.
        framed_participant_ids = list(dict.fromkeys(framed_participant_ids))
        requested_camera_action = str(values.get("camera_action") or "cut")
        # B1's lipsync runtime must receive a stable native scene crop. Editorial
        # moves such as slow_push are applied later by the timeline/render stage,
        # never inside the source clip submitted to MuseTalk.
        camera_action = "cut"
        input_payload: dict = {
            "scene_artifact_id": scene_reference,
            "speaker_participant_id": speaker_id,
            "framed_participant_ids": framed_participant_ids,
            "camera_view": b1_camera_view,
            "camera": {
                "view": b1_camera_view,
                "action": camera_action,
                "composition": "native_scene_camera",
            },
            "seating_plan": values.get("seating_plan"),
        }
        face_regions = studio_panel.get("face_regions") or studio_panel.get("seat_map")
        selected_face_regions = self._selected_panel_face_regions(
            face_regions,
            framed_participant_ids,
        )
        if not selected_face_regions:
            raise ValueError(
                "seated panel lipsync requires normalized face regions for its framed participants"
            )
        missing_framed_participants = [
            participant_id
            for participant_id in framed_participant_ids
            if participant_id
            not in {
                str(region.get("participant_id") or "")
                for region in selected_face_regions
            }
        ]
        if missing_framed_participants:
            raise ValueError(
                "seated panel lipsync is missing normalized face regions for: "
                + ", ".join(missing_framed_participants)
            )
        input_payload["face_regions"] = selected_face_regions
        wall_screen_asset_id = values.get("wall_screen_broll_asset_id")
        wall_screen_metadata: dict = {}
        if isinstance(wall_screen_asset_id, str) and wall_screen_asset_id:
            wall_screen_asset = next(
                (
                    candidate
                    for candidate in episode.assets
                    if str(candidate.id) == wall_screen_asset_id
                ),
                None,
            )
            if (
                wall_screen_asset is None
                or wall_screen_asset.status != "completed"
                or not wall_screen_asset.storage_uri
                or wall_screen_asset.generation_metadata.get("render_ready") is False
            ):
                raise ValueError("seated panel lipsync is waiting for its wall-screen media")
            wall_payload = self._private_object_bytes(
                wall_screen_asset.storage_uri, "studio wall-screen media"
            )
            wall_reference = await self._upload_b1_private_media(
                client,
                api_base,
                payload=wall_payload,
                content_type=wall_screen_asset.mime_type or "image/png",
                field="wall_screen",
            )
            input_payload["wall_screen_artifact_id"] = wall_reference
            wall_screen_metadata = {
                "asset_id": wall_screen_asset_id,
                "reference": wall_reference,
                "sha256": hashlib.sha256(wall_payload).hexdigest(),
            }
        return input_payload, {
            "seated_panel": {
                "scene_keyframe_asset_id": scene_asset_id,
                "scene_reference": scene_reference,
                "scene_reference_source": scene_source,
                "scene_sha256": (
                    hashlib.sha256(scene_payload).hexdigest() if scene_payload is not None else None
                ),
                "speaker_participant_id": speaker_id,
                "framed_participant_ids": framed_participant_ids,
                "camera_view": requested_camera_view,
                "b1_camera_view": b1_camera_view,
                "camera_action": camera_action,
                "requested_camera_action": requested_camera_action,
                "camera_composition": "native_scene_camera",
                "seating_plan": values.get("seating_plan"),
                "wall_screen": wall_screen_metadata or None,
            }
        }

    @staticmethod
    def _selected_panel_face_regions(
        raw_face_regions: object,
        participant_ids: list[str],
    ) -> list[dict]:
        """Return ordered face-region entries for B1's native camera framing."""
        if isinstance(raw_face_regions, dict):
            raw_face_regions = [
                {"participant_id": participant_id, **candidate}
                for participant_id in participant_ids
                if isinstance((candidate := raw_face_regions.get(participant_id)), dict)
            ]
        if not isinstance(raw_face_regions, list):
            return []
        regions_by_id = {
            str(
                entry.get("participant_id")
                or entry.get("speaker_participant_id")
                or entry.get("id")
                or ""
            ): entry
            for entry in raw_face_regions
            if isinstance(entry, dict)
        }
        return [
            regions_by_id[participant_id]
            for participant_id in participant_ids
            if participant_id in regions_by_id
        ]

    def _b1_character_performance_plan(self, asset: Asset) -> dict:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        raw_performance = (
            prompt_inputs.get("performance")
            if isinstance(prompt_inputs, dict)
            else None
        )
        performance = CharacterPerformance.model_validate(
            raw_performance if isinstance(raw_performance, dict) else {}
        )
        return {
            "schema_version": "dialecticore.character_performance.v1",
            **performance.model_dump(),
        }

    async def _upload_b1_private_media(
        self,
        client: httpx.AsyncClient,
        api_base: str,
        *,
        payload: bytes,
        content_type: str,
        field: str,
    ) -> str:
        response = await client.post(
            f"{api_base}/v1/media/uploads",
            content=payload,
            headers={"content-type": content_type, "x-b1-field": field},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                error_payload = self._safe_provider_response_payload(response.json())
            except ValueError:
                error_payload = response.text[:500]
            raise ValueError(
                f"B1 {field} upload failed with HTTP {response.status_code}: {error_payload}"
            ) from exc
        body = response.json()
        reference = body.get("reference") if isinstance(body, dict) else None
        if isinstance(reference, str) and reference:
            return reference
        if isinstance(reference, dict):
            reference_id = reference.get("id") or reference.get("reference_id")
            if isinstance(reference_id, str) and reference_id:
                return reference_id
        raise ValueError(f"B1 {field} upload response did not include a reference id")

    def _b1_lipsync_portrait_uri(self, asset: Asset) -> str:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        values = prompt_inputs if isinstance(prompt_inputs, dict) else {}
        for key in ("portrait_reference_image_uri", "reference_image_uri"):
            uri = values.get(key)
            if isinstance(uri, str) and uri:
                return uri
        raise ValueError("talking-head lipsync requires a portrait reference image")

    def _refresh_target_visual_profile_references(
        self,
        episode: Episode,
        assets: list[Asset],
        visual_profiles: list[VisualProfile],
    ) -> None:
        """Refresh references for deliberately generated assets from their live profile."""
        profile_by_id = {profile.id: profile for profile in visual_profiles if profile.enabled}
        for asset in assets:
            profile_id = asset.generation_metadata.get("visual_profile_id")
            profile = profile_by_id.get(profile_id) if isinstance(profile_id, str) else None
            if profile is None:
                continue
            visual_role = str(asset.generation_metadata.get("visual_role") or "video_primary")
            existing_inputs = asset.generation_metadata.get("prompt_inputs")
            transcript_text = (
                str(existing_inputs.get("transcript_text") or "")
                if isinstance(existing_inputs, dict)
                else ""
            )
            prompt_inputs = {
                **(existing_inputs if isinstance(existing_inputs, dict) else {}),
                **self._visual_prompt_inputs(
                    episode,
                    transcript_text,
                    profile,
                    visual_role=visual_role,
                ),
            }
            portrait_uri = prompt_inputs.get("portrait_reference_image_uri")
            asset.generation_metadata = {
                **asset.generation_metadata,
                "prompt_inputs": prompt_inputs,
                "portrait_reference_uri": portrait_uri,
            }
            # A subsequent B1 submission will calculate a new checksum from the
            # refreshed private object, rather than exposing stale reference data.
            asset.generation_metadata.pop("portrait_sha256", None)

    def _private_object_bytes(self, uri: str, label: str) -> bytes:
        path = self.object_store.path_for_uri(uri)
        if path is None or not path.is_file():
            raise ValueError(f"{label} must be available in private object storage")
        return path.read_bytes()

    def _finalize_streamed_wav_for_b1(self, payload: bytes) -> bytes:
        """Replace streamed WAV length sentinels without changing PCM payload bytes."""
        if len(payload) < 20 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
            return payload

        offset = 12
        while offset + 8 <= len(payload):
            chunk_id = payload[offset : offset + 4]
            chunk_size = struct.unpack_from("<I", payload, offset + 4)[0]
            if chunk_id == b"data":
                if chunk_size != 0xFFFFFFFF and payload[4:8] != b"\xff\xff\xff\xff":
                    return payload
                data_size = len(payload) - (offset + 8)
                if data_size < 0 or data_size > 0xFFFFFFFF:
                    return payload
                finalized = bytearray(payload)
                struct.pack_into("<I", finalized, 4, len(payload) - 8)
                struct.pack_into("<I", finalized, offset + 4, data_size)
                return bytes(finalized)
            if chunk_size == 0xFFFFFFFF:
                return payload
            offset += 8 + chunk_size + (chunk_size % 2)
        return payload

    def _image_content_type_for_uri(self, uri: str) -> str:
        content_type = mimetypes.guess_type(uri)[0]
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("talking-head lipsync portrait must be PNG, JPEG, or WebP")
        return content_type

    def _prepare_b1_image_for_upload(
        self,
        payload: bytes,
        content_type: str,
    ) -> tuple[bytes, str, bool]:
        """Preserve canonical image bytes for authenticated B1 uploads.

        B1's current staged-upload endpoint accepts RGBA PNG references and
        retains their declared MIME type and checksum. Older deployments
        classified those uploads as generic binary data, so this method once
        converted every alpha PNG to an unqualified baseline JPEG. That
        destroyed alpha and reduced multi-megabyte identity references to tens
        of kilobytes before inference. Keep the compatibility return shape, but
        do not transform a supported image format here.
        """
        normalized_content_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("B1 image upload must be PNG, JPEG, or WebP")
        return payload, normalized_content_type, False

    def _wav_duration_ms(self, payload: bytes) -> int:
        try:
            with wave.open(io.BytesIO(payload), "rb") as wav_file:
                return round(wav_file.getnframes() / wav_file.getframerate() * 1000)
        except (wave.Error, EOFError) as exc:
            raise ValueError("talking-head lipsync dialogue asset is not a readable WAV") from exc

    def _managed_media_prompt_context(
        self,
        workflow: ComfyUiWorkflow,
        asset: Asset,
    ) -> dict:
        prompt_inputs = dict(asset.generation_metadata.get("prompt_inputs", {}))
        context = {
            **workflow.default_parameters,
            **prompt_inputs,
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type.value,
            "visual_role": asset.generation_metadata.get("visual_role"),
            "shot_type": asset.generation_metadata.get("shot_type"),
            "character_name": asset.generation_metadata.get("character_name"),
            "width": asset.width,
            "height": asset.height,
            "fps": asset.fps,
            "duration_ms": asset.duration_ms,
            "frame_count": self._frame_count_from_duration(asset.duration_ms, asset.fps),
        }
        context["positive_prompt"] = self._render_prompt_template(
            workflow.prompt_template.get("positive"),
            context,
        )
        context["negative_prompt"] = self._render_prompt_template(
            workflow.prompt_template.get("negative"),
            context,
        )
        return context

    async def _b1_managed_media_payload(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        workflow: ComfyUiWorkflow,
        asset: Asset,
        context: dict,
    ) -> tuple[dict, dict]:
        model = str(workflow.default_parameters.get("b1_media_preset") or "").strip()
        if not model:
            raise ValueError("B1 managed media workflow requires b1_media_preset")
        if (
            asset.generation_metadata.get("visual_role")
            in {"studio_scene", "studio_group_cutaway", "reaction_loop"}
            and model == "video-text"
        ):
            # Existing installations can retain the older text-to-video studio
            # workflow. Directed coverage needs the private studio/character
            # reference, so upgrade this request to B1's image-conditioned path.
            model = "video-image"
        modality = "video" if model.startswith("video-") else "image"
        operation = self._b1_managed_operation(model, asset)
        is_video = modality == "video"
        width = int(
            workflow.default_parameters.get("b1_media_width")
            or (512 if is_video else context.get("width") or 512)
        )
        height = int(
            workflow.default_parameters.get("b1_media_height")
            or (512 if is_video else context.get("height") or 512)
        )
        fps = int(
            workflow.default_parameters.get("b1_media_fps")
            or (12 if is_video else context.get("fps") or 12)
        )
        duration_ms = int(
            workflow.default_parameters.get("b1_media_duration_ms")
            or (3000 if is_video else context.get("duration_ms") or 3000)
        )
        if model == "video-image":
            # B1 publishes low-VRAM Wan VACE limits. Persisted workflows can
            # still contain the former 512px/4s defaults, so normalize them at
            # submission time as well as in the built-in workflow definitions.
            width = min(max(width, 256), 384)
            height = min(max(height, 256), 288)
            fps = min(max(fps, 4), 12)
            frame_count = min(
                max(self._frame_count_from_duration(duration_ms, fps), 5),
                33,
            )
            duration_ms = round(frame_count * 1000 / fps)
        input_payload = {
            "prompt": context.get("positive_prompt") or context.get("prompt") or "",
            "negative_prompt": context.get("negative_prompt") or "",
            "width": width,
            "height": height,
            "fps": fps,
            "duration_ms": duration_ms,
            "frame_count": self._frame_count_from_duration(duration_ms, fps),
            "seed": context.get("seed"),
            "steps": context.get("steps"),
            "cfg": context.get("cfg"),
        }
        if model == "video-image":
            # The published B1 VACE workflow owns its inference settings. The
            # legacy local workflow values exceed B1's low-VRAM limits and are
            # not part of the managed image-to-video input contract.
            input_payload.pop("steps")
            input_payload.pop("cfg")
        reference_values = {
            key: context.get(key)
            for key in (
            "reference_image_uri",
            "portrait_reference_image_uri",
            "full_body_reference_image_uri",
            "wardrobe_reference_image_uri",
            "show_scene_reference_image_uri",
            )
        }
        group_references = context.get("group_reference_image_uris")
        if isinstance(group_references, list):
            reference_values["group_reference_image_uris"] = group_references
        uploads = (
            await self._upload_b1_video_image_reference(
                client=client,
                api_base=api_base,
                asset=asset,
                references=reference_values,
            )
            if model == "video-image"
            else await self._upload_b1_managed_media_references(
                client=client,
                api_base=api_base,
                workflow=workflow,
                references=reference_values,
            )
        )
        input_payload.update(uploads["input"])
        payload = {
            "modality": modality,
            "operation": operation,
            "model": model,
            "input": {
                key: value for key, value in input_payload.items() if value is not None
            },
            "priority": str(
                workflow.default_parameters.get("b1_media_priority")
                or ("single_video" if is_video else "single_image")
            ),
            "runtime_policy": str(
                workflow.default_parameters.get("b1_media_runtime_policy") or "comfyui"
            ),
        }
        return payload, {"b1_upload_references": uploads["metadata"]}

    async def _upload_b1_video_image_reference(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        asset: Asset,
        references: dict[str, object],
    ) -> dict:
        """Stage B1 VACE's single private source image and submit its reference."""
        payload, content_type, source_field, source_count = self._b1_video_image_source(
            asset=asset,
            references=references,
        )
        reference = await self._upload_b1_private_media(
            client,
            api_base,
            payload=payload,
            content_type=content_type,
            field="image",
        )
        return {
            "input": {"source_image_artifact_id": reference},
            "metadata": {
                "schema_version": "dialecticore.b1_staged_source_image.v1",
                "transport": "authenticated_staged_upload",
                "source_fields": {
                    source_field: {
                        "count": source_count,
                        "content_type": content_type,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
                "input_fields": ["source_image_artifact_id"],
            },
        }

    def _b1_video_image_source(
        self,
        *,
        asset: Asset,
        references: dict[str, object],
    ) -> tuple[bytes, str, str, int]:
        """Return one private image for B1's documented single-image VACE input."""
        group_uris = references.get("group_reference_image_uris")
        visual_role = str(asset.generation_metadata.get("visual_role") or "")
        if visual_role == "studio_group_cutaway" and isinstance(group_uris, list):
            source_uris = [uri for uri in group_uris if isinstance(uri, str) and uri]
            if source_uris:
                payload, content_type = self._compose_b1_group_source_image(source_uris)
                payload, content_type = self._prepare_b1_vace_source(
                    payload,
                    content_type,
                )
                return payload, content_type, "group_reference_image_uris", len(source_uris)

        for source_key in (
            "reference_image_uri",
            "portrait_reference_image_uri",
            "full_body_reference_image_uri",
            "show_scene_reference_image_uri",
            "wardrobe_reference_image_uri",
        ):
            raw_uri = references.get(source_key)
            if not isinstance(raw_uri, str) or not raw_uri:
                continue
            payload, content_type = self._load_b1_reference_image(raw_uri, source_key)
            return payload, content_type, source_key, 1
        raise ValueError("B1 video-image generation requires a private source image")

    def _load_b1_reference_image(self, uri: str, source_key: str) -> tuple[bytes, str]:
        if not uri.startswith("object://"):
            raise ValueError(f"B1 managed media reference {source_key} must be stored privately")
        payload = self._private_object_bytes(uri, f"B1 {source_key} reference")
        content_type = self._image_content_type_for_uri(uri)
        return self._prepare_b1_vace_source(payload, content_type)

    def _prepare_b1_vace_source(
        self,
        payload: bytes,
        content_type: str,
    ) -> tuple[bytes, str]:
        """Produce B1's low-VRAM, opaque 256px PNG VACE source."""
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                (
                    "scale=256:256:force_original_aspect_ratio=decrease,"
                    "pad=256:256:(ow-iw)/2:(oh-ih)/2:color=0x18202aff"
                ),
                "-pix_fmt",
                "rgb24",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            input=payload,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(
                "unable to normalize VACE source image to a 256px PNG for B1"
                + (f": {detail[:300]}" if detail else "")
            )
        return completed.stdout, "image/png"

    def _compose_b1_group_source_image(self, source_uris: list[str]) -> tuple[bytes, str]:
        """Create a neutral contact sheet for B1's single-image VACE input."""
        source_paths: list[Path] = []
        for index, uri in enumerate(source_uris[:6]):
            if not uri.startswith("object://"):
                raise ValueError("B1 group reference images must be stored privately")
            path = self.object_store.path_for_uri(uri)
            if path is None or not path.is_file():
                raise ValueError(f"B1 group reference image {index + 1} is unavailable")
            self._image_content_type_for_uri(uri)
            source_paths.append(path)
        if not source_paths:
            raise ValueError("B1 group cutaway requires at least one private character reference")
        if len(source_paths) == 1:
            return self._load_b1_reference_image(
                source_uris[0],
                "group_reference_image_uris",
            )

        columns = min(3, len(source_paths))
        rows = math.ceil(len(source_paths) / columns)
        # Keep the composite itself within the documented low-VRAM VACE source
        # profile. The image remains a private upload; no character references
        # are exposed through a public URI.
        # ffmpeg may round an odd target up while preserving a portrait's
        # aspect ratio (for example 85px becomes 86px). Keep each cell even so
        # the scaled frame can never exceed its subsequent pad dimensions.
        cell_width = max(2, (256 // columns) // 2 * 2)
        cell_height = max(2, (256 // rows) // 2 * 2)
        filters = []
        for index in range(len(source_paths)):
            filters.append(
                f"[{index}:v]scale={cell_width}:{cell_height}:force_original_aspect_ratio=decrease,"
                f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2:color=0x18202aff[v{index}]"
            )
        layout = "|".join(
            f"{(index % columns) * cell_width}_{(index // columns) * cell_height}"
            for index in range(len(source_paths))
        )
        stacked_inputs = "".join(f"[v{index}]" for index in range(len(source_paths)))
        filters.append(
            f"{stacked_inputs}xstack=inputs={len(source_paths)}:layout={layout}:"
            "fill=0x18202aff,format=yuvj420p[group]"
        )
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                *[argument for path in source_paths for argument in ("-i", str(path))],
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[group]",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(
                "unable to compose B1 group source image"
                + (f": {detail[:300]}" if detail else "")
            )
        return completed.stdout, "image/jpeg"

    async def _upload_b1_managed_media_references(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        workflow: ComfyUiWorkflow,
        references: dict[str, object],
    ) -> dict:
        """Translate private object-store references into B1 upload references.

        B1 deliberately cannot dereference this application's `object://` URIs.
        The B1 API receives only upload ids; source paths remain local metadata.
        """
        uploads: dict[str, list[str]] = {}
        for source_key, raw_value in references.items():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for raw_uri in values:
                if not isinstance(raw_uri, str) or not raw_uri:
                    continue
                if not raw_uri.startswith("object://"):
                    raise ValueError(
                        f"B1 managed media reference {source_key} must be stored privately"
                    )
                payload = self._private_object_bytes(raw_uri, f"B1 {source_key} reference")
                content_type = self._image_content_type_for_uri(raw_uri)
                payload, content_type, _ = self._prepare_b1_image_for_upload(
                    payload,
                    content_type,
                )
                reference = await self._upload_b1_private_media(
                    client,
                    api_base,
                    payload=payload,
                    content_type=content_type,
                    field="image",
                )
                uploads.setdefault(source_key, []).append(reference)

        input_payload: dict[str, object] = {}
        reference_field_map = workflow.default_parameters.get("b1_reference_artifact_fields")
        field_map = reference_field_map if isinstance(reference_field_map, dict) else {}
        primary_source_keys = (
            "reference_image_uri",
            "portrait_reference_image_uri",
            "full_body_reference_image_uri",
            "show_scene_reference_image_uri",
            "wardrobe_reference_image_uri",
            "group_reference_image_uris",
        )
        primary_reference = next(
            (
                uploads[key][0]
                for key in primary_source_keys
                if uploads.get(key)
            ),
            None,
        )
        default_field = str(field_map.get("default") or "source_image")
        # Older persisted DialectiCore workflows predate B1's native VACE
        # request contract. Keep them runnable while the configuration records
        # are upgraded through the UI/API.
        if (
            workflow.default_parameters.get("b1_media_preset") == "video-image"
            and default_field == "image_artifact_id"
        ):
            default_field = "source_image"
        if primary_reference:
            input_payload[default_field] = primary_reference
        for source_key, reference_ids in uploads.items():
            configured_field = field_map.get(source_key)
            if not isinstance(configured_field, str) or not configured_field:
                continue
            input_payload[configured_field] = (
                reference_ids if source_key == "group_reference_image_uris" else reference_ids[0]
            )
        if uploads.get("group_reference_image_uris"):
            input_payload["reference_artifact_ids"] = uploads["group_reference_image_uris"]
        return {
            "input": input_payload,
            "metadata": {
                "schema_version": "dialecticore.b1_private_reference_uploads.v1",
                "source_fields": {
                    source_key: {"count": len(reference_ids), "references": reference_ids}
                    for source_key, reference_ids in uploads.items()
                },
                "input_fields": sorted(input_payload),
            },
        }

    def _b1_managed_operation(self, model: str, asset: Asset) -> str:
        if model == "image-upscale":
            return "upscaling"
        if model == "image-edit":
            return "image-edit"
        if model == "video-image":
            return "image-to-video"
        if model == "video-text":
            return "video-generation"
        if asset.asset_type == AssetType.broll:
            return "image-generation"
        return "image-generation"

    def _patched_api_workflow(
        self,
        workflow: ComfyUiWorkflow,
        asset: Asset,
    ) -> tuple[dict, dict, list[dict]]:
        prompt = deepcopy(workflow.api_workflow)
        prompt_inputs = dict(asset.generation_metadata.get("prompt_inputs", {}))
        context = {
            **workflow.default_parameters,
            **prompt_inputs,
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type.value,
            "visual_role": asset.generation_metadata.get("visual_role"),
            "shot_type": asset.generation_metadata.get("shot_type"),
            "character_name": asset.generation_metadata.get("character_name"),
            "width": asset.width,
            "height": asset.height,
            "fps": asset.fps,
            "duration_ms": asset.duration_ms,
        }
        context["frame_count"] = self._frame_count_from_duration(
            context.get("duration_ms"),
            context.get("fps"),
        )
        context["positive_prompt"] = self._render_prompt_template(
            workflow.prompt_template.get("positive"),
            context,
        )
        context["negative_prompt"] = self._render_prompt_template(
            workflow.prompt_template.get("negative"),
            context,
        )

        bindings = self._normalized_node_input_bindings(workflow)
        applied: list[dict] = []
        if bindings:
            for path, value_key in bindings.items():
                if value_key not in context or context[value_key] is None:
                    continue
                if self._set_workflow_path(prompt, path, context[value_key]):
                    applied.append({"path": path, "value_key": value_key})
        else:
            applied.extend(self._apply_common_workflow_input_names(prompt, context))
        return prompt, context, applied

    def _render_prompt_template(self, template: object, context: dict) -> str:
        if not isinstance(template, str) or not template:
            return ""
        return template.format_map(_PromptContext(context))

    def _frame_count_from_duration(
        self,
        duration_ms: object,
        fps: object,
    ) -> int | None:
        duration = self._optional_float(duration_ms)
        frame_rate = self._optional_float(fps)
        if duration is None or frame_rate is None or duration <= 0 or frame_rate <= 0:
            return None
        return max(1, round((duration / 1000) * frame_rate))

    def _normalized_node_input_bindings(self, workflow: ComfyUiWorkflow) -> dict[str, str]:
        raw_bindings = workflow.prompt_template.get("node_input_bindings")
        if not isinstance(raw_bindings, dict):
            raw_bindings = workflow.default_parameters.get("node_input_bindings")
        if not isinstance(raw_bindings, dict):
            return {}
        normalized: dict[str, str] = {}
        for key, value in raw_bindings.items():
            if isinstance(value, str):
                normalized[str(key)] = value
            elif isinstance(value, list):
                for path in value:
                    if isinstance(path, str):
                        normalized[path] = str(key)
        return normalized

    def _set_workflow_path(self, prompt: dict, path: str, value: object) -> bool:
        parts = [part for part in path.split(".") if part]
        if len(parts) < 2:
            return False
        target: object = prompt
        for part in parts[:-1]:
            if isinstance(target, dict) and part in target:
                target = target[part]
                continue
            return False
        if isinstance(target, dict):
            target[parts[-1]] = value
            return True
        return False

    def _apply_common_workflow_input_names(self, prompt: dict, context: dict) -> list[dict]:
        applied: list[dict] = []
        input_name_map = {
            "text": "positive_prompt",
            "prompt": "positive_prompt",
            "positive": "positive_prompt",
            "positive_prompt": "positive_prompt",
            "negative": "negative_prompt",
            "negative_prompt": "negative_prompt",
            "width": "width",
            "height": "height",
            "fps": "fps",
            "seed": "seed",
            "reference_image": "reference_image_uri",
            "reference_image_uri": "reference_image_uri",
            "image_reference": "reference_image_uri",
            "character_reference": "reference_image_uri",
            "character_reference_image": "reference_image_uri",
            "reference_image_download_url": "reference_image_download_url",
            "character_reference_download_url": "reference_image_download_url",
            "character_reference_image_download_url": "reference_image_download_url",
            "portrait_reference": "portrait_reference_image_uri",
            "portrait_reference_image": "portrait_reference_image_uri",
            "portrait_reference_image_uri": "portrait_reference_image_uri",
            "portrait_reference_download_url": "portrait_reference_image_download_url",
            "portrait_reference_image_download_url": "portrait_reference_image_download_url",
            "full_body_reference": "full_body_reference_image_uri",
            "full_body_reference_image": "full_body_reference_image_uri",
            "full_body_reference_image_uri": "full_body_reference_image_uri",
            "full_body_reference_download_url": "full_body_reference_image_download_url",
            "full_body_reference_image_download_url": "full_body_reference_image_download_url",
            "wardrobe_reference": "wardrobe_reference_image_uri",
            "wardrobe_reference_image": "wardrobe_reference_image_uri",
            "wardrobe_reference_image_uri": "wardrobe_reference_image_uri",
            "wardrobe_reference_download_url": "wardrobe_reference_image_download_url",
            "wardrobe_reference_image_download_url": "wardrobe_reference_image_download_url",
            "show_scene_reference": "show_scene_reference_image_uri",
            "show_scene_reference_image": "show_scene_reference_image_uri",
            "show_scene_reference_image_uri": "show_scene_reference_image_uri",
            "show_scene_reference_download_url": "show_scene_reference_image_download_url",
            "show_scene_reference_image_download_url": "show_scene_reference_image_download_url",
        }
        for node_id, node in prompt.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for input_name in list(inputs):
                value_key = input_name_map.get(input_name.lower())
                if value_key is None or context.get(value_key) is None:
                    continue
                inputs[input_name] = context[value_key]
                applied.append(
                    {
                        "path": f"{node_id}.inputs.{input_name}",
                        "value_key": value_key,
                    }
                )
        return applied

    async def _poll_visual_result(
        self,
        endpoint: ComfyUiEndpoint,
        asset: Asset,
        job_id: str,
    ) -> VisualResult:
        if not endpoint.base_url:
            raise ValueError("comfyui endpoint requires base_url")

        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        path_template = endpoint.capabilities.get("job_status_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = endpoint.capabilities.get("history_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = (
                "/v1/media/jobs/{job_id}"
                if self._is_b1_managed_media_asset(asset)
                else "/history/{job_id}"
            )
        status_path = path_template.format(job_id=job_id, asset_id=asset.id)
        base_url = self._remote_job_api_base(endpoint, asset)
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.get(f"{base_url.rstrip('/')}{status_path}")
            response.raise_for_status()
            data = response.json()
        return self._visual_result_from_payload(
            endpoint=endpoint,
            asset=asset,
            payload=data,
            default_job_id=job_id,
            default_status="running",
        )

    async def _cancel_existing_job_before_retry(
        self,
        endpoint: ComfyUiEndpoint,
        asset: Asset,
    ) -> bool:
        job_id = asset.generation_metadata.get("remote_job_id")
        if endpoint.adapter_type == "mock" or not endpoint.base_url or not job_id:
            return True
        try:
            cancel_response = await self._cancel_visual_job(endpoint, asset, str(job_id))
        except httpx.HTTPError as exc:
            asset.status = "failed"
            asset.generation_metadata = {
                **asset.generation_metadata,
                "status": "failed",
                "failure": "remote cancellation before retry failed",
                "last_cancel_error": str(exc),
                "last_cancel_attempted_at": datetime.now(UTC).isoformat(),
            }
            asset.updated_at = datetime.now(UTC)
            return False
        cancel_count = int(asset.generation_metadata.get("cancellation_attempt_count", 0))
        asset.generation_metadata = {
            **asset.generation_metadata,
            "status": "planned",
            "remote_job_id": None,
            "cancelled_remote_job_id": job_id,
            "remote_cancelled": True,
            "remote_cancel_response": self._safe_provider_response_payload(cancel_response),
            "remote_job_cancellation_required": False,
            "cancellation_attempt_count": cancel_count + 1,
            "cancelled_at": datetime.now(UTC).isoformat(),
            "ready_for_retry": True,
        }
        asset.updated_at = datetime.now(UTC)
        return True

    def _requires_cancel_before_retry(self, asset: Asset) -> bool:
        if asset.status in {"submitted", "running"}:
            return True
        return bool(
            asset.generation_metadata.get("remote_job_id")
            and asset.generation_metadata.get("remote_job_cancellation_required")
        )

    async def _cancel_visual_job(
        self,
        endpoint: ComfyUiEndpoint,
        asset: Asset,
        job_id: str,
    ) -> dict:
        if not endpoint.base_url:
            raise ValueError("comfyui endpoint requires base_url")

        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        path_template = endpoint.capabilities.get("job_cancel_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = endpoint.capabilities.get("cancellation_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = (
                "/v1/media/jobs/{job_id}"
                if self._is_b1_managed_media_asset(asset)
                else "/queue/{job_id}"
            )
        cancel_path = path_template.format(job_id=job_id, asset_id=asset.id)
        base_url = self._remote_job_api_base(endpoint, asset)
        method = endpoint.capabilities.get("job_cancel_method")
        if not isinstance(method, str) or not method:
            method = endpoint.capabilities.get("cancellation_method")
        normalized_method = (method if isinstance(method, str) and method else "DELETE").upper()
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        payload = {"asset_id": str(asset.id), "job_id": job_id}
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            if normalized_method == "POST":
                response = await client.post(
                    f"{base_url.rstrip('/')}{cancel_path}",
                    json=payload,
                )
            else:
                response = await client.request(
                    normalized_method,
                    f"{base_url.rstrip('/')}{cancel_path}",
                )
            response.raise_for_status()
            if not response.content:
                return {"status": "cancelled"}
            try:
                data = response.json()
            except ValueError:
                return {"status": "cancelled"}
            return data if isinstance(data, dict) else {"status": "cancelled"}

    def _remote_job_api_base(self, endpoint: ComfyUiEndpoint, asset: Asset) -> str:
        managed_base = str(
            asset.generation_metadata.get("managed_media_api_base")
            or asset.generation_metadata.get("b1_managed_api_base")
            or ""
        ).strip()
        if managed_base:
            return managed_base
        return endpoint.base_url or ""

    def _is_b1_managed_media_asset(self, asset: Asset) -> bool:
        return bool(
            asset.generation_metadata.get("adapter") == "b1_managed_media"
            or asset.generation_metadata.get("managed_media_api_base")
            or asset.generation_metadata.get("b1_managed_api_base")
        )

    def _visual_result_from_payload(
        self,
        endpoint: ComfyUiEndpoint,
        asset: Asset,
        payload: dict,
        default_job_id: object | None,
        default_status: str,
    ) -> VisualResult:
        media_bytes = self._media_bytes_from_payload(payload)
        storage_uri = self._absolute_result_uri(
            endpoint,
            payload,
            self._result_storage_uri(payload)
            or self._comfyui_view_uri(
                endpoint,
                payload,
            ),
        )
        status_default = "completed" if media_bytes is not None or storage_uri else default_status
        status = self._normalize_visual_status(
            payload.get("status", payload.get("state", payload.get("b1_status", status_default)))
        )
        artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        first_artifact = next((item for item in artifacts if isinstance(item, dict)), {})
        mime_type = (
            payload.get("mime_type")
            or first_artifact.get("mime_type")
            or self._default_visual_mime_type(asset)
        )
        lip_sync = payload.get("lip_sync") if isinstance(payload.get("lip_sync"), dict) else {}
        performance = (
            payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
        )
        studio_panel = (
            payload.get("studio_panel") if isinstance(payload.get("studio_panel"), dict) else {}
        )
        seated_character = (
            payload.get("seated_character")
            if isinstance(payload.get("seated_character"), dict)
            else {}
        )
        provider_lip_sync_offset_ms = self._optional_int(lip_sync.get("measured_offset_ms"))
        provider_lip_sync_duration_ms = self._optional_int(lip_sync.get("duration_ms"))
        provider_lip_sync_fps = self._optional_float(lip_sync.get("fps"))
        is_managed_b1_job = self._is_b1_managed_media_asset(asset)
        provider_failure_category = payload.get("failure_category")
        provider_failure_message = payload.get("failure_message") or payload.get("error")
        provider_failure = (
            str(provider_failure_message)
            if status == "failed" and provider_failure_message is not None
            else None
        )
        return VisualResult(
            status=status,
            storage_uri=storage_uri,
            mime_type=mime_type,
            duration_ms=payload.get("duration_ms", asset.duration_ms),
            width=payload.get("width", asset.width),
            height=payload.get("height", asset.height),
            fps=payload.get("fps", asset.fps),
            checksum=payload.get("checksum"),
            metadata={
                "adapter": "b1_managed_media" if is_managed_b1_job else "comfyui",
                "adapter_type": endpoint.adapter_type,
                "comfyui_endpoint_id": endpoint.id,
                **(
                    {"managed_media_api_base": self._remote_job_api_base(endpoint, asset)}
                    if is_managed_b1_job
                    else {}
                ),
                "remote_job_id": payload.get("prompt_id")
                or payload.get("job_id")
                or payload.get("b1_job_id")
                or payload.get("id")
                or default_job_id,
                "remote_status": status,
                "lip_sync": self._safe_provider_response_payload(lip_sync),
                "lip_sync_mode": lip_sync.get("mode"),
                "provider_audio_sha256": lip_sync.get("audio_sha256"),
                "provider_timing_sha256": lip_sync.get("timing_sha256"),
                "provider_lip_sync_offset_ms": provider_lip_sync_offset_ms,
                "provider_lip_sync_duration_ms": provider_lip_sync_duration_ms,
                "provider_lip_sync_fps": provider_lip_sync_fps,
                "provider_lip_sync_backend": lip_sync.get("backend"),
                "provider_musetalk_commit": lip_sync.get("musetalk_commit"),
                "studio_panel": self._safe_provider_response_payload(studio_panel),
                "seated_character": self._safe_provider_response_payload(seated_character),
                "provider_seated_reference_artifact_id": (
                    seated_character.get("seated_reference_artifact_id")
                    or seated_character.get("reference_id")
                    or seated_character.get("artifact_id")
                    or seated_character.get("reference")
                ),
                "provider_studio_panel_camera_view": studio_panel.get("camera_view"),
                "provider_studio_panel_actual_camera_view": studio_panel.get(
                    "actual_camera_view"
                ),
                "provider_studio_panel_camera_composition": studio_panel.get(
                    "camera_composition"
                ),
                "provider_studio_panel_speaker_face_region_px": (
                    self._safe_provider_response_payload(
                        studio_panel.get("speaker_face_region_px")
                    )
                    if isinstance(studio_panel.get("speaker_face_region_px"), dict)
                    else None
                ),
                "provider_studio_panel_framed_participant_ids": (
                    [
                        str(participant_id)
                        for participant_id in studio_panel.get("framed_participant_ids", [])
                        if isinstance(participant_id, str) and participant_id
                    ]
                    if isinstance(studio_panel.get("framed_participant_ids"), list)
                    else []
                ),
                "provider_studio_panel_seat_map": self._safe_provider_response_payload(
                    studio_panel.get("seat_map")
                    if isinstance(studio_panel.get("seat_map"), list)
                    else []
                ),
                "provider_wall_screen_quad": self._safe_provider_response_payload(
                    studio_panel.get("wall_screen_quad")
                    if isinstance(studio_panel.get("wall_screen_quad"), list)
                    else []
                ),
                "performance": self._safe_provider_response_payload(performance),
                "provider_performance_mode": performance.get("mode"),
                "provider_performance_applied": performance.get("applied") is True,
                "provider_failure_category": provider_failure_category,
                "provider_failure_message": provider_failure,
                **({"failure": provider_failure} if provider_failure else {}),
                "provider_response": self._safe_provider_response_payload(payload),
            },
            media_bytes=media_bytes,
        )

    async def _materialize_visual_result(
        self,
        endpoint: ComfyUiEndpoint | None,
        asset: Asset,
        result: VisualResult,
    ) -> VisualResult:
        media_bytes = result.media_bytes
        if media_bytes is None and result.storage_uri and endpoint is not None:
            media_bytes = await self._download_remote_visual_result(endpoint, result.storage_uri)
        if media_bytes is None:
            return result
        stored = self.object_store.put_bytes(
            self._visual_object_key(asset, result.mime_type),
            media_bytes,
            result.mime_type or "application/octet-stream",
        )
        probe = self._probe_visual_path(stored.path, result.mime_type)
        metadata = {
            **result.metadata,
            "storage_backend": stored.backend,
            "object_storage_key": stored.key,
            "object_storage_path": str(stored.path),
            "object_size_bytes": stored.size_bytes,
            "media_probe": self._visual_probe_metadata(probe),
        }
        if metadata.get("render_ready") is None:
            metadata["render_ready"] = probe.render_ready
        return VisualResult(
            status=result.status,
            storage_uri=stored.uri,
            mime_type=probe.mime_type or result.mime_type or stored.content_type,
            duration_ms=probe.duration_ms or result.duration_ms,
            width=probe.width or result.width,
            height=probe.height or result.height,
            fps=probe.fps or result.fps,
            checksum=stored.checksum,
            metadata=metadata,
            media_bytes=None,
        )

    async def _download_remote_visual_result(
        self,
        endpoint: ComfyUiEndpoint,
        result_uri: str,
    ) -> bytes | None:
        if not self._is_downloadable_result_uri(endpoint, result_uri):
            return None
        headers: dict[str, str] = {}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token and (
            endpoint.capabilities.get("result_download_include_authorization")
            or self._is_api_hub_result_uri(endpoint, result_uri)
        ):
            headers["authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=endpoint.default_timeout_seconds,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.get(result_uri)
            response.raise_for_status()
            return response.content

    def _reference_image_mime_type(self, content_type: str) -> str:
        mime_type = content_type.split(";", maxsplit=1)[0].strip().lower()
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("reference image must be PNG, JPEG, or WebP")
        return mime_type

    def _validate_reference_image_payload(self, payload: bytes, mime_type: str) -> None:
        if mime_type == "image/png" and payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return
        if mime_type == "image/jpeg" and payload.startswith(b"\xff\xd8\xff"):
            return
        if (
            mime_type == "image/webp"
            and len(payload) >= 12
            and payload[:4] == b"RIFF"
            and payload[8:12] == b"WEBP"
        ):
            return
        raise ValueError("reference image content does not match declared image type")

    def _reference_image_extension(self, mime_type: str, filename: str) -> str:
        extension_by_type = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }
        declared = extension_by_type[mime_type]
        suffix = Path(filename).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            return ".jpg" if suffix == ".jpeg" else suffix
        return declared

    def _endpoint_verify(self, endpoint: ComfyUiEndpoint) -> bool | str:
        ca_path = str(endpoint.capabilities.get("tls_ca_cert_path") or "").strip()
        if ca_path and not Path(ca_path).is_file():
            raise ValueError("configured ComfyUI CA certificate file is not available")
        return ca_path or True

    def _ca_certificate_storage_path(self, endpoint: ComfyUiEndpoint) -> Path:
        configured = str(endpoint.capabilities.get("tls_ca_cert_path") or "").strip()
        filename = Path(configured).name if configured else ""
        if not filename:
            safe_endpoint_id = "".join(
                character if character.isalnum() or character in {"-", "_"} else "-"
                for character in endpoint.id
            ).strip("-")
            filename = f"{safe_endpoint_id or 'comfyui'}-ca.crt"
        return Path(self.settings.runtime_state_path) / "certificates" / filename

    def _is_downloadable_result_uri(
        self,
        endpoint: ComfyUiEndpoint,
        result_uri: str,
    ) -> bool:
        parsed = urlparse(result_uri)
        if parsed.scheme not in {"http", "https"}:
            return False
        if endpoint.capabilities.get("allow_external_result_urls"):
            return True
        if not endpoint.base_url:
            return False
        base = urlparse(endpoint.base_url)
        if parsed.scheme == base.scheme and parsed.netloc == base.netloc:
            return True
        api_base = str(endpoint.capabilities.get("remote_nodes_api_base") or "").strip()
        if api_base:
            parsed_api_base = urlparse(api_base)
            return (
                parsed.scheme == parsed_api_base.scheme
                and parsed.netloc == parsed_api_base.netloc
            )
        return False

    def _is_api_hub_result_uri(self, endpoint: ComfyUiEndpoint, result_uri: str) -> bool:
        api_base = str(endpoint.capabilities.get("remote_nodes_api_base") or "").strip()
        if not api_base:
            return False
        parsed = urlparse(result_uri)
        parsed_api_base = urlparse(api_base)
        return parsed.scheme == parsed_api_base.scheme and parsed.netloc == parsed_api_base.netloc

    def _mock_visual_result(
        self,
        endpoint: ComfyUiEndpoint,
        workflow: ComfyUiWorkflow,
        asset: Asset,
    ) -> VisualResult:
        width = asset.width or 1280
        height = asset.height or 720
        prompt_inputs = asset.generation_metadata.get("prompt_inputs", {})
        transcript_text = (
            str(prompt_inputs.get("transcript_text", ""))
            if isinstance(prompt_inputs, dict)
            else ""
        )
        title = str(
            asset.generation_metadata.get("character_name")
            or asset.generation_metadata.get("visual_role")
            or "Mock visual"
        )
        media_bytes = self._render_fallback_svg(
            width=width,
            height=height,
            title=title,
            body=transcript_text or f"{workflow.name} mock visual",
            footer=f"{asset.generation_metadata.get('shot_type') or asset.asset_type.value}",
        )
        return VisualResult(
            status="completed",
            storage_uri=None,
            mime_type="image/svg+xml",
            duration_ms=asset.duration_ms,
            width=width,
            height=height,
            fps=None,
            checksum=None,
            metadata={
                "adapter": "comfyui",
                "adapter_type": endpoint.adapter_type,
                "comfyui_endpoint_id": endpoint.id,
                "remote_job_id": f"mock-comfyui-{asset.id}",
                "deterministic_mock_visual": True,
                "render_ready": True,
                "requires_static_image_duration": True,
            },
            media_bytes=media_bytes,
        )

    def _citation_card_visual_result(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        asset: Asset,
    ) -> VisualResult:
        width = asset.width or episode.definition.media.width
        height = asset.height or episode.definition.media.height
        media_bytes = self._render_citation_card_svg(
            width=width,
            height=height,
            title="Evidence",
            entries=asset.generation_metadata.get("citation_entries", []),
            transcript_text=self._transcript_text_for_asset(transcript, asset),
        )
        return VisualResult(
            status="completed",
            storage_uri=None,
            mime_type="image/svg+xml",
            duration_ms=asset.duration_ms,
            width=width,
            height=height,
            fps=None,
            checksum=None,
            metadata={
                "adapter": "dialecticore-citation-card",
                "deterministic_citation_card": True,
                "citation_overlay": True,
                "render_ready": True,
                "requires_static_image_duration": True,
            },
            media_bytes=media_bytes,
        )

    def _wall_screen_card_visual_result(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        asset: Asset,
    ) -> VisualResult:
        """Render source-bound rear-screen coverage without a generative model."""
        width = asset.width or 512
        height = asset.height or 288
        transcript_text = self._fallback_visual_body(transcript, asset)
        title = self._wall_screen_card_title(transcript_text)
        media_bytes = self._render_wall_screen_card_png(
            width=width,
            height=height,
            title=title,
            body=transcript_text,
        )
        return VisualResult(
            status="completed",
            storage_uri=None,
            mime_type="image/png",
            duration_ms=asset.duration_ms,
            width=width,
            height=height,
            fps=None,
            checksum=None,
            metadata={
                "adapter": "dialecticore-wall-screen-card-png/v1",
                "deterministic_wall_screen_card": True,
                "source_bound_visual": True,
                "render_ready": True,
                "requires_static_image_duration": True,
            },
            media_bytes=media_bytes,
        )

    def _render_wall_screen_card_png(
        self,
        *,
        width: int,
        height: int,
        title: str,
        body: str,
    ) -> bytes:
        """Rasterize the deterministic SVG card to a B1-compatible opaque PNG."""
        svg = self._render_wall_screen_card_svg(
            width=width,
            height=height,
            title=title,
            body=body,
        )
        with tempfile.NamedTemporaryFile(suffix=".svg") as source:
            source.write(svg)
            source.flush()
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    source.name,
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                capture_output=True,
                check=False,
            )
        if completed.returncode != 0 or not completed.stdout:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(
                "unable to rasterize deterministic wall-screen card"
                + (f": {detail[:300]}" if detail else "")
            )
        return completed.stdout

    @staticmethod
    def _wall_screen_card_title(transcript_text: str) -> str:
        normalized = transcript_text.casefold()
        themes = (
            (("abwärme", "wärme", "nahwärme"), "Abwärme nutzen"),
            (("wasser", "kühl"), "Wasser & Kühlung"),
            (("job", "wertschöpfung", "kommune"), "Lokale Wertschöpfung"),
            (("netz", "strom", "last"), "Netz & Flexibilität"),
        )
        for keywords, title in themes:
            if any(keyword in normalized for keyword in keywords):
                return title
        return "KI-Rechenzentren"

    def _render_wall_screen_card_svg(
        self,
        *,
        width: int,
        height: int,
        title: str,
        body: str,
    ) -> bytes:
        body_lines = self._wrapped_svg_lines(body, max_chars=52, max_lines=3)
        title_size = max(24, int(height * 0.14))
        body_size = max(14, int(height * 0.067))
        body_start = int(height * 0.52)
        body_step = max(20, int(height * 0.10))
        body_nodes = "\n".join(
            (
                f'<text x="{int(width * 0.08)}" y="{body_start + index * body_step}" '
                'font-family="Inter, Arial, sans-serif" '
                f'font-size="{body_size}" fill="#dbeafe">{escape(line)}</text>'
            )
            for index, line in enumerate(body_lines)
        )
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
  viewBox="0 0 {width} {height}">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#071426"/><stop offset="1" stop-color="#102a43"/>
  </linearGradient>
</defs>
<rect width="{width}" height="{height}" rx="12" fill="url(#bg)"/>
<rect x="{int(width * 0.045)}" y="{int(height * 0.07)}" width="{int(width * 0.91)}"
  height="{int(height * 0.86)}" rx="12" fill="none" stroke="#22d3ee" stroke-width="3"/>
<text x="{int(width * 0.08)}" y="{int(height * 0.18)}"
  font-family="Inter, Arial, sans-serif" font-size="{max(11, int(height * 0.045))}"
  font-weight="700" fill="#67e8f9">DIALECTICORE · THEMENKARTE</text>
<text x="{int(width * 0.08)}" y="{int(height * 0.39)}"
  font-family="Inter, Arial, sans-serif" font-size="{title_size}"
  font-weight="800" fill="#f8fafc">{escape(title)}</text>
<line x1="{int(width * 0.08)}" y1="{int(height * 0.45)}" x2="{int(width * 0.92)}"
  y2="{int(height * 0.45)}" stroke="#0ea5e9" stroke-width="3"/>
{body_nodes}
<circle cx="{int(width * 0.88)}" cy="{int(height * 0.18)}"
  r="{max(5, int(height * 0.025))}" fill="#22d3ee"/>
</svg>
""".encode()

    def _fallback_visual_result(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        asset: Asset,
        reason: str,
        source_status: str,
        provider_metadata: dict | None = None,
    ) -> VisualResult:
        width = asset.width or episode.definition.media.width
        height = asset.height or episode.definition.media.height
        fallback_kind = (
            "fallback_still"
            if asset.generation_metadata.get("visual_role") == "broll"
            else "citation_card"
        )
        title = self._fallback_visual_title(episode, asset, fallback_kind)
        body = self._fallback_visual_body(transcript, asset)
        media_bytes = self._render_fallback_svg(
            width=width,
            height=height,
            title=title,
            body=body,
            footer=f"{asset.generation_metadata.get('shot_type') or asset.asset_type.value}",
        )
        return VisualResult(
            status="completed",
            storage_uri=None,
            mime_type="image/svg+xml",
            duration_ms=asset.duration_ms,
            width=width,
            height=height,
            fps=asset.fps,
            checksum=None,
            metadata={
                "adapter": "dialecticore-fallback",
                "fallback_visual": True,
                "fallback_kind": fallback_kind,
                "fallback_reason": reason,
                "fallback_source_status": source_status,
                "fallback_provider_metadata": self._safe_provider_response_payload(
                    provider_metadata or {}
                ),
                "render_ready": True,
                "requires_static_image_duration": True,
            },
            media_bytes=media_bytes,
        )

    def _fallback_visual_title(
        self,
        episode: Episode,
        asset: Asset,
        fallback_kind: str,
    ) -> str:
        if fallback_kind == "citation_card":
            character_name = asset.generation_metadata.get("character_name")
            if isinstance(character_name, str) and character_name:
                return f"{character_name}"
        prompt_inputs = asset.generation_metadata.get("prompt_inputs", {})
        topic = prompt_inputs.get("topic") if isinstance(prompt_inputs, dict) else None
        return str(topic or episode.central_question)

    def _fallback_visual_body(self, transcript: TranscriptVersion, asset: Asset) -> str:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs", {})
        if isinstance(prompt_inputs, dict):
            transcript_text = prompt_inputs.get("transcript_text")
            if isinstance(transcript_text, str) and transcript_text:
                return transcript_text
        for turn in transcript.turns:
            if str(turn.id) == asset.source_entity_id:
                return turn.text
        return "Fallback visual card"

    def _render_fallback_svg(
        self,
        width: int,
        height: int,
        title: str,
        body: str,
        footer: str,
    ) -> bytes:
        title_lines = self._wrapped_svg_lines(title, max_chars=38, max_lines=2)
        body_lines = self._wrapped_svg_lines(body, max_chars=56, max_lines=6)
        title_nodes = "\n".join(
            (
                f'<text x="{width // 2}" y="{int(height * 0.28) + index * 74}" '
                'text-anchor="middle" font-family="Inter, Arial, sans-serif" '
                f'font-size="60" font-weight="700" fill="#f8fafc">{escape(line)}</text>'
            )
            for index, line in enumerate(title_lines)
        )
        body_start_y = int(height * 0.48)
        body_nodes = "\n".join(
            (
                f'<text x="{width // 2}" y="{body_start_y + index * 52}" '
                'text-anchor="middle" font-family="Inter, Arial, sans-serif" '
                f'font-size="38" fill="#d1d5db">{escape(line)}</text>'
            )
            for index, line in enumerate(body_lines)
        )
        card_x = width * 0.08
        card_y = height * 0.12
        card_width = width * 0.84
        card_height = height * 0.76
        label_y = int(height * 0.18)
        footer_y = int(height * 0.82)
        footer_text = escape(footer)
        return f"""<svg xmlns="http://www.w3.org/2000/svg"
  width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="{width}" height="{height}" fill="#111827"/>
<rect x="{card_x:.0f}" y="{card_y:.0f}" width="{card_width:.0f}"
  height="{card_height:.0f}" rx="18" fill="#1f2937" stroke="#475569" stroke-width="3"/>
<text x="{width // 2}" y="{label_y}" text-anchor="middle"
  font-family="Inter, Arial, sans-serif" font-size="28" fill="#93c5fd">
  DialectiCore fallback visual
</text>
{title_nodes}
{body_nodes}
<text x="{width // 2}" y="{footer_y}" text-anchor="middle"
  font-family="Inter, Arial, sans-serif" font-size="26" fill="#9ca3af">{footer_text}</text>
</svg>
""".encode()

    def _render_citation_card_svg(
        self,
        width: int,
        height: int,
        title: str,
        entries: object,
        transcript_text: str,
    ) -> bytes:
        normalized_entries = [
            entry for entry in entries if isinstance(entry, dict)
        ] if isinstance(entries, list) else []
        source_labels = []
        claim_lines = []
        for entry in normalized_entries[:3]:
            source_label = entry.get("source_title") or entry.get("evidence_ref")
            if isinstance(source_label, str) and source_label:
                source_labels.append(source_label)
            claim = entry.get("claim")
            if isinstance(claim, str) and claim:
                claim_lines.extend(self._wrapped_svg_lines(claim, max_chars=68, max_lines=2))
        if not claim_lines:
            claim_lines = self._wrapped_svg_lines(transcript_text, max_chars=68, max_lines=3)
        source_text = " | ".join(source_labels[:3]) or "Evidence source"
        title_nodes = "\n".join(
            (
                f'<text x="{int(width * 0.08)}" y="{int(height * 0.18) + index * 64}" '
                'font-family="Inter, Arial, sans-serif" font-size="54" '
                f'font-weight="700" fill="#f8fafc">{escape(line)}</text>'
            )
            for index, line in enumerate(
                self._wrapped_svg_lines(title, max_chars=28, max_lines=2)
            )
        )
        claim_nodes = "\n".join(
            (
                f'<text x="{int(width * 0.08)}" y="{int(height * 0.39) + index * 48}" '
                'font-family="Inter, Arial, sans-serif" font-size="34" '
                f'fill="#dbeafe">{escape(line)}</text>'
            )
            for index, line in enumerate(claim_lines[:5])
        )
        source_nodes = "\n".join(
            (
                f'<text x="{int(width * 0.08)}" y="{int(height * 0.78) + index * 38}" '
                'font-family="Inter, Arial, sans-serif" font-size="28" '
                f'fill="#bae6fd">{escape(line)}</text>'
            )
            for index, line in enumerate(
                self._wrapped_svg_lines(source_text, max_chars=74, max_lines=2)
            )
        )
        inset_x = int(width * 0.08)
        card_x = int(width * 0.045)
        card_y = int(height * 0.10)
        card_width = int(width * 0.91)
        card_height = int(height * 0.80)
        line_y = int(height * 0.30)
        line_end_x = int(width * 0.92)
        label_y = card_y + 52
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
  viewBox="0 0 {width} {height}">
<rect width="{width}" height="{height}" fill="#020617"/>
<rect x="{card_x}" y="{card_y}" width="{card_width}"
  height="{card_height}" rx="18" fill="#0f172a" stroke="#38bdf8" stroke-width="4"/>
<text x="{inset_x}" y="{label_y}" font-family="Inter, Arial, sans-serif"
  font-size="24" fill="#7dd3fc">DialectiCore citation overlay</text>
{title_nodes}
<line x1="{inset_x}" y1="{line_y}" x2="{line_end_x}" y2="{line_y}"
  stroke="#1e40af" stroke-width="3"/>
{claim_nodes}
<text x="{int(width * 0.08)}" y="{int(height * 0.72)}"
  font-family="Inter, Arial, sans-serif" font-size="24" fill="#67e8f9">Sources</text>
{source_nodes}
</svg>
""".encode()

    def _wrapped_svg_lines(
        self,
        value: str,
        max_chars: int,
        max_lines: int,
    ) -> list[str]:
        words = " ".join(value.split()).split()
        if not words:
            return []
        lines: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and len(candidate) > max_chars:
                lines.append(" ".join(current))
                current = [word]
                if len(lines) == max_lines:
                    break
                continue
            current.append(word)
        if current and len(lines) < max_lines:
            lines.append(" ".join(current))
        if len(lines) == max_lines and len(" ".join(words)) > len(" ".join(lines)):
            lines[-1] = lines[-1].rstrip(".") + "..."
        return lines

    def _probe_visual_path(
        self,
        path: Path,
        fallback_mime_type: str | None,
    ) -> VisualProbeResult:
        if not path.exists():
            return VisualProbeResult(
                mime_type=fallback_mime_type,
                width=None,
                height=None,
                duration_ms=None,
                fps=None,
                frame_count=None,
                size_bytes=None,
                probe_tool="filesystem",
                probe_warnings=["stored visual object not found"],
                render_ready=False,
            )
        size_bytes = path.stat().st_size
        if fallback_mime_type == "application/vnd.dialecticore.visual-placeholder+json":
            return VisualProbeResult(
                mime_type=fallback_mime_type,
                width=None,
                height=None,
                duration_ms=None,
                fps=None,
                frame_count=None,
                size_bytes=size_bytes,
                probe_tool="placeholder",
                probe_warnings=[],
                render_ready=False,
            )
        header = path.read_bytes()[:64]
        png_dimensions = self._png_dimensions(header)
        if png_dimensions is not None:
            width, height = png_dimensions
            pixel_analysis = self._analyze_png_pixels(path, width, height)
            return VisualProbeResult(
                mime_type="image/png",
                width=width,
                height=height,
                duration_ms=None,
                fps=None,
                frame_count=1,
                size_bytes=size_bytes,
                probe_tool="image_header",
                probe_warnings=[],
                render_ready=True,
                pixel_analysis=pixel_analysis,
            )
        jpeg_dimensions = self._jpeg_dimensions(path)
        if jpeg_dimensions is not None:
            width, height = jpeg_dimensions
            return VisualProbeResult(
                mime_type="image/jpeg",
                width=width,
                height=height,
                duration_ms=None,
                fps=None,
                frame_count=1,
                size_bytes=size_bytes,
                probe_tool="image_header",
                probe_warnings=[],
                render_ready=True,
            )
        svg_dimensions = self._svg_dimensions(path)
        if svg_dimensions is not None:
            width, height = svg_dimensions
            pixel_analysis = self._analyze_svg_structure(path)
            return VisualProbeResult(
                mime_type="image/svg+xml",
                width=width,
                height=height,
                duration_ms=None,
                fps=None,
                frame_count=1,
                size_bytes=size_bytes,
                probe_tool="svg_header",
                probe_warnings=[],
                render_ready=True,
                pixel_analysis=pixel_analysis,
            )
        if fallback_mime_type and fallback_mime_type.startswith("video/"):
            return self._probe_video_path(path, fallback_mime_type)
        return VisualProbeResult(
            mime_type=fallback_mime_type,
            width=None,
            height=None,
            duration_ms=None,
            fps=None,
            frame_count=None,
            size_bytes=size_bytes,
            probe_tool="filesystem",
            probe_warnings=["unsupported visual media probe"],
            render_ready=False,
        )

    def _probe_video_path(self, path: Path, fallback_mime_type: str | None) -> VisualProbeResult:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return VisualProbeResult(
                mime_type=fallback_mime_type,
                width=None,
                height=None,
                duration_ms=None,
                fps=None,
                frame_count=None,
                size_bytes=path.stat().st_size,
                probe_tool="none",
                probe_warnings=["ffprobe not available for video probe"],
                render_ready=False,
            )
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size,bit_rate,format_name:"
                "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,"
                "nb_frames,duration,bit_rate,pix_fmt"
            ),
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            payload = json.loads(completed.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            return VisualProbeResult(
                mime_type=fallback_mime_type,
                width=None,
                height=None,
                duration_ms=None,
                fps=None,
                frame_count=None,
                size_bytes=path.stat().st_size,
                probe_tool="ffprobe",
                probe_warnings=[f"ffprobe failed: {exc}"],
                render_ready=False,
            )
        video_stream = next(
            (
                stream
                for stream in payload.get("streams", [])
                if stream.get("codec_type") == "video"
            ),
            None,
        )
        media_format = payload.get("format", {})
        duration = self._optional_float(media_format.get("duration"))
        if duration is None and video_stream is not None:
            duration = self._optional_float(video_stream.get("duration"))
        width = self._optional_int(video_stream.get("width")) if video_stream else None
        height = self._optional_int(video_stream.get("height")) if video_stream else None
        fps = self._fps_from_rate(video_stream.get("r_frame_rate")) if video_stream else None
        if fps is None and video_stream is not None:
            fps = self._fps_from_rate(video_stream.get("avg_frame_rate"))
        frame_count = self._optional_int(video_stream.get("nb_frames")) if video_stream else None
        estimated_frame_count = (
            max(1, round(duration * fps)) if duration is not None and fps else None
        )
        frame_count_source = "ffprobe_nb_frames" if frame_count is not None else None
        warnings = []
        critical_warnings = []
        if video_stream is None:
            warnings.append("video probe missing video stream")
            critical_warnings.append("video probe missing video stream")
        elif not video_stream.get("codec_name"):
            warnings.append("video probe missing codec")
        if video_stream is not None and not video_stream.get("pix_fmt"):
            warnings.append("video probe missing pixel format")
        if width is None or height is None or width <= 0 or height <= 0:
            warnings.append("video probe missing dimensions")
            critical_warnings.append("video probe missing dimensions")
        if duration is None or duration <= 0:
            warnings.append("video probe missing duration")
            critical_warnings.append("video probe missing duration")
        if fps is None or fps <= 0:
            warnings.append("video probe missing fps")
            critical_warnings.append("video probe missing fps")
        if frame_count is None:
            if estimated_frame_count is not None:
                frame_count = estimated_frame_count
                frame_count_source = "estimated_from_duration_and_fps"
                warnings.append("video probe missing exact frame count")
            else:
                warnings.append("video probe missing frame count")
                critical_warnings.append("video probe missing frame count")
        elif frame_count <= 0:
            warnings.append("video probe nonpositive frame count")
            critical_warnings.append("video probe nonpositive frame count")
        if (
            frame_count is not None
            and frame_count > 0
            and estimated_frame_count is not None
            and abs(frame_count - estimated_frame_count) > max(2, estimated_frame_count * 0.15)
        ):
            warnings.append("video probe frame count duration mismatch")
        video_analysis = {
            "analysis_tool": "ffprobe_video_integrity",
            "format_name": media_format.get("format_name"),
            "codec_name": video_stream.get("codec_name") if video_stream else None,
            "pixel_format": video_stream.get("pix_fmt") if video_stream else None,
            "container_bit_rate": self._optional_int(media_format.get("bit_rate")),
            "stream_bit_rate": (
                self._optional_int(video_stream.get("bit_rate")) if video_stream else None
            ),
            "estimated_frame_count": estimated_frame_count,
            "frame_count_source": frame_count_source,
            "critical_warnings": critical_warnings,
            "warning_count": len(warnings),
        }
        return VisualProbeResult(
            mime_type=fallback_mime_type,
            width=width,
            height=height,
            duration_ms=int(duration * 1000) if duration is not None else None,
            fps=fps,
            frame_count=frame_count,
            size_bytes=self._optional_int(media_format.get("size")) or path.stat().st_size,
            probe_tool="ffprobe",
            probe_warnings=warnings,
            render_ready=not critical_warnings,
            video_analysis=video_analysis,
        )

    def _visual_probe_metadata(self, probe: VisualProbeResult) -> dict:
        return {
            "mime_type": probe.mime_type,
            "width": probe.width,
            "height": probe.height,
            "duration_ms": probe.duration_ms,
            "fps": probe.fps,
            "frame_count": probe.frame_count,
            "size_bytes": probe.size_bytes,
            "probe_tool": probe.probe_tool,
            "probe_warnings": probe.probe_warnings,
            "render_ready": probe.render_ready,
            "pixel_analysis": probe.pixel_analysis,
            "video_analysis": probe.video_analysis,
        }

    def _safe_provider_response_payload(self, payload: object) -> object:
        return safe_provider_response_payload(payload)

    def _is_sensitive_provider_response_key(self, key: str) -> bool:
        return is_sensitive_provider_response_key(key)

    def _png_dimensions(self, header: bytes) -> tuple[int, int] | None:
        if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        if header[12:16] != b"IHDR":
            return None
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width <= 0 or height <= 0:
            return None
        return width, height

    def _jpeg_dimensions(self, path: Path) -> tuple[int, int] | None:
        data = path.read_bytes()
        if len(data) < 4 or not data.startswith(b"\xff\xd8"):
            return None
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(data):
                return None
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                if width > 0 and height > 0:
                    return width, height
                return None
            index += segment_length
        return None

    def _svg_dimensions(self, path: Path) -> tuple[int, int] | None:
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError):
            return None
        if not root.tag.endswith("svg"):
            return None
        width = self._svg_length(root.attrib.get("width"))
        height = self._svg_length(root.attrib.get("height"))
        if width is not None and height is not None:
            return width, height
        view_box = root.attrib.get("viewBox")
        if not isinstance(view_box, str):
            return None
        parts = view_box.replace(",", " ").split()
        if len(parts) != 4:
            return None
        parsed_width = self._optional_float(parts[2])
        parsed_height = self._optional_float(parts[3])
        if not parsed_width or not parsed_height:
            return None
        return int(parsed_width), int(parsed_height)

    def _svg_length(self, value: object) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        normalized = value.strip().removesuffix("px")
        parsed = self._optional_float(normalized)
        if not parsed or parsed <= 0:
            return None
        return int(parsed)

    def _analyze_png_pixels(self, path: Path, width: int, height: int) -> dict:
        analysis = {
            "analysis_tool": "png_pixels",
            "pixel_warnings": [],
        }
        try:
            data = path.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                return {
                    **analysis,
                    "supported": False,
                    "unsupported_reason": "not a png file",
                }
            idat_chunks: list[bytes] = []
            bit_depth: int | None = None
            color_type: int | None = None
            interlace_method: int | None = None
            index = 8
            while index + 12 <= len(data):
                chunk_length = int.from_bytes(data[index : index + 4], "big")
                chunk_type = data[index + 4 : index + 8]
                chunk_data_start = index + 8
                chunk_data_end = chunk_data_start + chunk_length
                chunk_end = chunk_data_end + 4
                if chunk_length < 0 or chunk_end > len(data):
                    return {
                        **analysis,
                        "supported": False,
                        "unsupported_reason": "truncated png chunk",
                    }
                chunk_data = data[chunk_data_start:chunk_data_end]
                if chunk_type == b"IHDR" and len(chunk_data) >= 13:
                    bit_depth = chunk_data[8]
                    color_type = chunk_data[9]
                    interlace_method = chunk_data[12]
                elif chunk_type == b"IDAT":
                    idat_chunks.append(chunk_data)
                elif chunk_type == b"IEND":
                    break
                index = chunk_end

            channel_counts = {0: 1, 2: 3, 6: 4}
            if bit_depth != 8 or color_type not in channel_counts or interlace_method != 0:
                return {
                    **analysis,
                    "supported": False,
                    "unsupported_reason": (
                        "only non-interlaced 8-bit grayscale, rgb, and rgba pngs are analyzed"
                    ),
                    "bit_depth": bit_depth,
                    "color_type": color_type,
                    "interlace_method": interlace_method,
                }
            bytes_per_pixel = channel_counts[color_type]
            row_bytes = width * bytes_per_pixel
            expected_minimum = height * (row_bytes + 1)
            if expected_minimum > 40_000_000:
                return {
                    **analysis,
                    "supported": False,
                    "unsupported_reason": "png pixel data exceeds qc analysis limit",
                    "estimated_raw_bytes": expected_minimum,
                }

            raw = zlib.decompress(b"".join(idat_chunks))
            if len(raw) < expected_minimum:
                return {
                    **analysis,
                    "supported": False,
                    "unsupported_reason": "png pixel data shorter than expected",
                    "raw_bytes": len(raw),
                    "expected_raw_bytes": expected_minimum,
                }

            total_pixels = width * height
            sample_stride = max(1, total_pixels // 200_000)
            sample_index = 0
            sampled_count = 0
            luma_sum = 0.0
            min_luma = 255.0
            max_luma = 0.0
            dark_count = 0
            transparent_count = 0
            unique_colors: set[tuple[int, ...]] = set()
            unique_capped = False
            previous_row = bytearray(row_bytes)
            offset = 0
            for _row_number in range(height):
                filter_type = raw[offset]
                offset += 1
                row = bytearray(raw[offset : offset + row_bytes])
                offset += row_bytes
                self._unfilter_png_row(row, previous_row, filter_type, bytes_per_pixel)
                for pixel_offset in range(0, row_bytes, bytes_per_pixel):
                    if sample_index % sample_stride == 0:
                        if color_type == 0:
                            red = green = blue = row[pixel_offset]
                            alpha = 255
                            color = (red,)
                        elif color_type == 2:
                            red, green, blue = row[pixel_offset : pixel_offset + 3]
                            alpha = 255
                            color = (red, green, blue)
                        else:
                            red, green, blue, alpha = row[pixel_offset : pixel_offset + 4]
                            color = (red, green, blue, alpha)
                        luma = (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)
                        luma_sum += luma
                        min_luma = min(min_luma, luma)
                        max_luma = max(max_luma, luma)
                        if luma < 16:
                            dark_count += 1
                        if alpha < 10:
                            transparent_count += 1
                        if len(unique_colors) <= 4096:
                            unique_colors.add(color)
                        else:
                            unique_capped = True
                        sampled_count += 1
                    sample_index += 1
                previous_row = row

            if sampled_count == 0:
                return {
                    **analysis,
                    "supported": False,
                    "unsupported_reason": "png had no sampled pixels",
                }
            average_luma = luma_sum / sampled_count
            luma_range = max_luma - min_luma
            dark_pixel_ratio = dark_count / sampled_count
            transparent_pixel_ratio = transparent_count / sampled_count
            low_detail = len(unique_colors) <= 2 or luma_range < 8
            mostly_dark = average_luma < 12 or dark_pixel_ratio > 0.98
            mostly_transparent = transparent_pixel_ratio > 0.95
            warnings = []
            if low_detail:
                warnings.append("png image has very low visual detail")
            if mostly_dark:
                warnings.append("png image is mostly dark or blank")
            if mostly_transparent:
                warnings.append("png image is mostly transparent")
            return {
                **analysis,
                "supported": True,
                "sampled_pixel_count": sampled_count,
                "sample_stride": sample_stride,
                "unique_color_count": len(unique_colors),
                "unique_color_count_capped": unique_capped,
                "average_luma": round(average_luma, 2),
                "min_luma": round(min_luma, 2),
                "max_luma": round(max_luma, 2),
                "luma_range": round(luma_range, 2),
                "dark_pixel_ratio": round(dark_pixel_ratio, 4),
                "transparent_pixel_ratio": round(transparent_pixel_ratio, 4),
                "low_detail": low_detail,
                "mostly_dark": mostly_dark,
                "mostly_transparent": mostly_transparent,
                "pixel_warnings": warnings,
            }
        except (OSError, ValueError, zlib.error) as exc:
            return {
                **analysis,
                "supported": False,
                "unsupported_reason": f"png pixel analysis failed: {exc}",
            }

    def _unfilter_png_row(
        self,
        row: bytearray,
        previous_row: bytearray,
        filter_type: int,
        bytes_per_pixel: int,
    ) -> None:
        if filter_type == 0:
            return
        if filter_type == 1:
            for index, value in enumerate(row):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (value + left) & 0xFF
            return
        if filter_type == 2:
            for index, value in enumerate(row):
                row[index] = (value + previous_row[index]) & 0xFF
            return
        if filter_type == 3:
            for index, value in enumerate(row):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous_row[index]
                row[index] = (value + ((left + up) // 2)) & 0xFF
            return
        if filter_type == 4:
            for index, value in enumerate(row):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous_row[index]
                upper_left = (
                    previous_row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                )
                row[index] = (value + self._png_paeth_predictor(left, up, upper_left)) & 0xFF
            return
        raise ValueError(f"unsupported png filter type {filter_type}")

    def _png_paeth_predictor(self, left: int, up: int, upper_left: int) -> int:
        estimate = left + up - upper_left
        left_distance = abs(estimate - left)
        up_distance = abs(estimate - up)
        upper_left_distance = abs(estimate - upper_left)
        if left_distance <= up_distance and left_distance <= upper_left_distance:
            return left
        if up_distance <= upper_left_distance:
            return up
        return upper_left

    def _analyze_svg_structure(self, path: Path) -> dict:
        analysis = {
            "analysis_tool": "svg_structure",
            "pixel_warnings": [],
        }
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError) as exc:
            return {
                **analysis,
                "supported": False,
                "unsupported_reason": f"svg structure analysis failed: {exc}",
            }
        elements = list(root.iter())
        text_nodes = [
            "".join(element.itertext()).strip()
            for element in elements
            if element.tag.split("}")[-1] in {"text", "tspan"}
            and "".join(element.itertext()).strip()
        ]
        visible_text_character_count = sum(len(value) for value in text_nodes)
        paint_count = 0
        visible_shape_count = 0
        for element in elements:
            tag = element.tag.split("}")[-1]
            if tag in {
                "circle",
                "ellipse",
                "image",
                "line",
                "path",
                "polygon",
                "polyline",
                "rect",
                "text",
                "tspan",
            }:
                visible_shape_count += 1
            for attribute in ("fill", "stroke"):
                paint = element.attrib.get(attribute)
                if isinstance(paint, str) and paint.strip().lower() not in {
                    "",
                    "none",
                    "transparent",
                }:
                    paint_count += 1

        low_detail = visible_shape_count <= 2 and visible_text_character_count < 24
        warnings = []
        if low_detail:
            warnings.append("svg visual has very low structural detail")
        if paint_count == 0:
            warnings.append("svg visual has no explicit visible paint")
        return {
            **analysis,
            "supported": True,
            "svg_element_count": len(elements),
            "visible_shape_count": visible_shape_count,
            "text_node_count": len(text_nodes),
            "visible_text_character_count": visible_text_character_count,
            "paint_count": paint_count,
            "low_detail": low_detail,
            "pixel_warnings": warnings,
        }

    def _native_seated_panel_coverage_failure(
        self,
        asset: Asset,
        result: VisualResult,
    ) -> str | None:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        if (
            asset.generation_metadata.get("visual_role") != "video_primary"
            or not isinstance(prompt_inputs, dict)
            or prompt_inputs.get("studio_layout") != "seated_panel"
            or result.status != "completed"
        ):
            return None
        planned_view = str(prompt_inputs.get("camera_view") or "speaker_medium")
        expected_view = {
            "speaker_close_up": "speaker_close",
        }.get(planned_view, planned_view)
        studio_panel = result.metadata.get("studio_panel")
        if not isinstance(studio_panel, dict):
            return "B1 completed seated-panel video without studio_panel coverage evidence"
        actual_view = str(studio_panel.get("actual_camera_view") or "")
        if actual_view != expected_view:
            return (
                "B1 camera coverage mismatch: requested "
                f"{expected_view}, received {actual_view or 'none'}"
            )
        if studio_panel.get("camera_composition") != "native_scene_camera":
            return "B1 seated-panel output is missing native_scene_camera evidence"
        if expected_view != "establishing_wide" and (
            (result.width or 0) < 1024 or (result.height or 0) < 576
        ):
            return (
                "B1 returned insufficient native camera output resolution: "
                f"{result.width or 0}x{result.height or 0}"
            )
        minimum_face_height = {
            "speaker_medium": 140,
            "speaker_close": 220,
            "panel_two_shot": 110,
        }.get(expected_view)
        if minimum_face_height is not None:
            face_region = studio_panel.get("speaker_face_region_px")
            if not isinstance(face_region, dict):
                return "B1 completed native camera output without speaker face-pixel evidence"
            face_height = self._optional_int(face_region.get("height")) or 0
            if face_height < minimum_face_height:
                return (
                    "B1 returned insufficient native camera face coverage: "
                    f"{face_height}px high, requires {minimum_face_height}px"
                )
        expected_framed = [
            participant_id
            for participant_id in [
                prompt_inputs.get("speaker_participant_id"),
                *(prompt_inputs.get("paired_participant_ids") or []),
            ]
            if isinstance(participant_id, str) and participant_id
        ]
        reported_framed = studio_panel.get("framed_participant_ids")
        if not isinstance(reported_framed, list) or not set(expected_framed).issubset(
            {str(participant_id) for participant_id in reported_framed}
        ):
            return "B1 native camera output is missing one or more framed participants"
        return None

    def _apply_visual_result(
        self,
        asset: Asset,
        result: VisualResult,
        submitted_by: str | None = None,
        synced_by: str | None = None,
    ) -> None:
        coverage_failure = self._native_seated_panel_coverage_failure(asset, result)
        if coverage_failure is not None:
            result = replace(
                result,
                status="failed",
                storage_uri=None,
                mime_type=None,
                checksum=None,
                metadata={
                    **result.metadata,
                    "render_ready": False,
                    "provider_failure_category": "native_camera_coverage_rejected",
                    "provider_failure_message": coverage_failure,
                    "failure": coverage_failure,
                    "native_camera_coverage_rejected": True,
                },
            )
        asset.status = result.status
        if result.storage_uri is not None:
            asset.storage_uri = result.storage_uri
        elif result.status in {"submitted", "running"}:
            asset.storage_uri = None
        if result.mime_type is not None:
            asset.mime_type = result.mime_type
        if result.duration_ms is not None:
            asset.duration_ms = result.duration_ms
        if result.width is not None:
            asset.width = result.width
        if result.height is not None:
            asset.height = result.height
        if result.fps is not None:
            asset.fps = result.fps
        if result.checksum is not None:
            asset.checksum = result.checksum
        elif result.status in {"submitted", "running"}:
            asset.checksum = None
        generation_count = int(asset.generation_metadata.get("generation_attempt_count", 0))
        sync_count = int(asset.generation_metadata.get("sync_attempt_count", 0))
        metadata = {
            **asset.generation_metadata,
            **result.metadata,
            "status": result.status,
            "remote_job_cancellation_required": result.status in {"submitted", "running"},
        }
        if result.status in {"submitted", "running"}:
            for key in (
                "failure",
                "failed_at",
                "ready_for_retry",
                "retry_exhausted",
                "provider_failure_category",
                "provider_failure_message",
                "last_sync_error",
                "media_probe",
                "object_storage_key",
                "object_storage_path",
                "object_size_bytes",
                "storage_backend",
                "render_ready",
                "completed_at",
            ):
                metadata.pop(key, None)
            if metadata.get("visual_role") == "studio_seated_character":
                for key in (
                    "approval_status",
                    "reviewed_at",
                    "reviewed_by",
                    "review_comment",
                    "b1_approval_response",
                ):
                    metadata.pop(key, None)
        if result.metadata.get("fallback_visual") is not True:
            for key in (
                "fallback_visual",
                "fallback_kind",
                "fallback_reason",
                "fallback_source_status",
                "fallback_provider_metadata",
            ):
                metadata.pop(key, None)
        if result.metadata.get("native_camera_coverage_rejected") is not True:
            metadata.pop("native_camera_coverage_rejected", None)
        if submitted_by is not None:
            metadata["generation_attempt_count"] = generation_count + 1
            metadata["submitted_by"] = submitted_by or "system"
            metadata["submitted_at"] = datetime.now(UTC).isoformat()
        if synced_by is not None:
            metadata["sync_attempt_count"] = sync_count + 1
            metadata["last_synced_by"] = synced_by or "system"
            metadata["last_synced_at"] = datetime.now(UTC).isoformat()
        if result.status == "completed":
            for key in (
                "failure",
                "provider_failure_category",
                "provider_failure_message",
            ):
                metadata.pop(key, None)
            metadata["completed_at"] = datetime.now(UTC).isoformat()
            if metadata.get("visual_role") == "studio_panel_keyframe":
                metadata.setdefault("approval_status", "pending_review")
            if metadata.get("visual_role") == "studio_seated_character":
                metadata.setdefault("approval_status", "pending_review")
        asset.generation_metadata = metadata
        asset.updated_at = datetime.now(UTC)

    def _visual_media_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        request: VisualQualityRequest,
        endpoints: list[ComfyUiEndpoint],
        workflows: list[ComfyUiWorkflow],
    ) -> QualityResult:
        assets = self._target_visual_assets(episode, transcript, request)
        skipped_optional_broll_asset_ids: list[str] = []
        if (
            request.asset_ids is None
            and request.transcript_turn_ids is None
            and not request.participant_ids
        ):
            # B-roll is deliberately optional in the studio directing plan:
            # no missing B-roll is referenced by a camera beat. Do not fail
            # global render-readiness QC on legacy or unproduced inserts, while
            # still inspecting every completed B-roll asset and explicit
            # editor-selected B-roll check.
            optional_unproduced_broll = [
                asset
                for asset in assets
                if asset.asset_type == AssetType.broll and asset.status != "completed"
            ]
            skipped_optional_broll_asset_ids = [
                str(asset.id) for asset in optional_unproduced_broll
            ]
            assets = [
                asset for asset in assets if asset not in optional_unproduced_broll
            ]
        workflow_by_id = {workflow.id: workflow for workflow in workflows if workflow.enabled}
        endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints if endpoint.enabled}
        issues: list[dict] = []
        completed_assets = [asset for asset in assets if asset.status == "completed"]
        dimension_checked_count = 0
        dimension_mismatch_count = 0
        fps_checked_count = 0
        duration_checked_count = 0
        duration_mismatch_count = 0
        lip_sync_ready_count = 0
        lip_sync_missing_count = 0
        lip_sync_measured_count = 0
        max_lip_sync_offset_ms = 0
        lip_sync_offset_sum_ms = 0
        style_checked_count = 0
        style_missing_count = 0
        style_consistency_checked_count = 0
        style_consistency_warning_count = 0
        identity_consistency_checked_count = 0
        identity_consistency_warning_count = 0
        probed_count = 0
        pixel_analyzed_count = 0
        pixel_warning_count = 0
        video_probe_checked_count = 0
        video_probe_warning_count = 0
        video_probe_invalid_count = 0
        video_probe_missing_frame_count = 0
        video_probe_estimated_frame_count = 0

        for asset in assets:
            if asset.status != "completed":
                issues.append(
                    self._visual_issue(asset, "fail", "visual_asset_not_completed")
                )
                continue
            if not asset.storage_uri:
                issues.append(
                    self._visual_issue(asset, "fail", "completed_visual_missing_storage")
                )
            if not asset.checksum:
                issues.append(
                    self._visual_issue(asset, "warning", "completed_visual_missing_checksum")
                )
            if not self._is_render_suitable_visual_asset(asset):
                issues.append(
                    self._visual_issue(asset, "warning", "visual_asset_not_render_suitable")
                )
            if asset.generation_metadata.get("fallback_visual") is True:
                issues.append(self._visual_issue(asset, "warning", "visual_fallback_used"))
            if asset.generation_metadata.get("mock_visual_placeholder") is True:
                issues.append(
                    self._visual_issue(asset, "warning", "visual_placeholder_not_render_ready")
                )

            probe = asset.generation_metadata.get("media_probe")
            if isinstance(probe, dict):
                probed_count += 1
                for warning in probe.get("probe_warnings", []):
                    issues.append(
                        {
                            **self._visual_issue(asset, "warning", "visual_media_probe_warning"),
                            "warning": warning,
                            "probe_tool": probe.get("probe_tool"),
                        }
                    )
                pixel_analysis = probe.get("pixel_analysis")
                if isinstance(pixel_analysis, dict):
                    pixel_analyzed_count += 1
                    pixel_warnings = [
                        warning
                        for warning in pixel_analysis.get("pixel_warnings", [])
                        if isinstance(warning, str)
                    ]
                    if pixel_warnings:
                        pixel_warning_count += 1
                    for warning in pixel_warnings:
                        issues.append(
                            {
                                **self._visual_issue(
                                    asset,
                                    "warning",
                                    "visual_pixel_analysis_warning",
                                ),
                                "warning": warning,
                                "analysis_tool": pixel_analysis.get("analysis_tool"),
                            }
                        )
                if self._requires_video_probe(asset):
                    video_probe_checked_count += 1
                    video_issues = self._video_probe_issues(asset, probe)
                    if video_issues:
                        video_probe_warning_count += 1
                        issues.extend(video_issues)
                    video_analysis = probe.get("video_analysis")
                    if isinstance(video_analysis, dict):
                        critical_warnings = video_analysis.get("critical_warnings")
                        if isinstance(critical_warnings, list) and critical_warnings:
                            video_probe_invalid_count += 1
                        if (
                            video_analysis.get("frame_count_source")
                            == "estimated_from_duration_and_fps"
                        ):
                            video_probe_estimated_frame_count += 1
                        probe_warnings = probe.get("probe_warnings")
                        if (
                            isinstance(probe_warnings, list)
                            and "video probe missing frame count" in probe_warnings
                        ):
                            video_probe_missing_frame_count += 1
            else:
                issues.append(self._visual_issue(asset, "warning", "visual_media_probe_missing"))

            expected_width, expected_height, expected_fps = self._visual_qc_expectations(
                asset,
                episode,
                workflow_by_id,
            )
            if asset.width is not None and asset.height is not None:
                dimension_checked_count += 1
                if (
                    asset.width != expected_width
                    or asset.height != expected_height
                ):
                    dimension_mismatch_count += 1
                    issues.append(
                        {
                            **self._visual_issue(asset, "warning", "visual_dimension_mismatch"),
                            "width": asset.width,
                            "height": asset.height,
                            "expected_width": expected_width,
                            "expected_height": expected_height,
                        }
                    )
            else:
                issues.append(self._visual_issue(asset, "warning", "visual_dimensions_missing"))

            if self._is_video_like_asset(asset):
                fps_checked_count += 1
                if not asset.fps:
                    issues.append(self._visual_issue(asset, "warning", "visual_fps_missing"))
                elif abs(float(asset.fps) - float(expected_fps)) > 0.01:
                    issues.append(
                        {
                            **self._visual_issue(asset, "warning", "visual_fps_mismatch"),
                            "fps": asset.fps,
                            "expected_fps": expected_fps,
                        }
                    )
                if not asset.duration_ms:
                    issues.append(
                        self._visual_issue(asset, "warning", "visual_duration_missing")
                    )

            if self._is_primary_turn_video(asset):
                audio_asset = self._audio_asset_for_visual(episode, transcript, asset)
                lip_sync_measurement = self._visual_lip_sync_measurement(
                    asset,
                    audio_asset,
                )
                if lip_sync_measurement is not None:
                    lip_sync_measured_count += 1
                    offset_ms = int(lip_sync_measurement["offset_ms"])
                    max_lip_sync_offset_ms = max(
                        max_lip_sync_offset_ms,
                        abs(offset_ms),
                    )
                    lip_sync_offset_sum_ms += abs(offset_ms)
                timing = (
                    audio_asset.generation_metadata.get("phoneme_timing")
                    if audio_asset is not None
                    else None
                )
                has_provider_lipsync_evidence = (
                    asset.generation_metadata.get("lip_sync_mode")
                    in {"audio_driven", "audio_driven_seated_panel"}
                    and bool(asset.generation_metadata.get("provider_audio_sha256"))
                    and self._optional_int(
                        asset.generation_metadata.get("provider_lip_sync_offset_ms")
                    ) is not None
                )
                if has_provider_lipsync_evidence or (
                    isinstance(timing, dict) and timing.get("ready_for_lipsync") is True
                ):
                    lip_sync_ready_count += 1
                else:
                    lip_sync_missing_count += 1
                    issues.append(
                        self._visual_issue(asset, "warning", "visual_lipsync_not_ready")
                    )
                if lip_sync_measurement is not None and asset.duration_ms:
                    duration_checked_count += 1
                    measured_audio_duration_ms = int(
                        lip_sync_measurement["audio_duration_ms"]
                    )
                    delta_ms = abs(int(asset.duration_ms) - measured_audio_duration_ms)
                    threshold_ms = max(500, episode.definition.quality.block_on_sync_error_ms)
                    if delta_ms > threshold_ms:
                        duration_mismatch_count += 1
                        issues.append(
                            {
                                **self._visual_issue(
                                    asset,
                                    "warning",
                                    "visual_audio_duration_mismatch",
                                ),
                                "duration_ms": asset.duration_ms,
                                "audio_duration_ms": measured_audio_duration_ms,
                                "delta_ms": delta_ms,
                                "threshold_ms": threshold_ms,
                                "lip_sync_offset_ms": (
                                    lip_sync_measurement["offset_ms"]
                                    if lip_sync_measurement is not None
                                    else None
                                ),
                            }
                        )

            if self._is_character_visual_asset(asset):
                style_checked_count += 1
                consistency = self._visual_character_consistency(asset)
                if consistency is not None:
                    identity_consistency_checked_count += 1
                    style_consistency_checked_count += 1
                    if consistency["identity_score"] < 1.0:
                        identity_consistency_warning_count += 1
                        issues.append(
                            {
                                **self._visual_issue(
                                    asset,
                                    "warning",
                                    "visual_character_identity_mismatch",
                                ),
                                **consistency,
                            }
                        )
                    if consistency["style_score"] < 1.0:
                        style_consistency_warning_count += 1
                        issues.append(
                            {
                                **self._visual_issue(
                                    asset,
                                    "warning",
                                    "visual_style_prompt_mismatch",
                                ),
                                **consistency,
                            }
                        )
                if not self._has_visual_style_metadata(asset):
                    style_missing_count += 1
                    issues.append(
                        self._visual_issue(asset, "warning", "visual_style_metadata_missing")
                    )

            workflow_id = asset.generation_metadata.get("comfyui_workflow_id")
            if workflow_id and str(workflow_id) not in workflow_by_id:
                issues.append(
                    {
                        **self._visual_issue(asset, "warning", "visual_workflow_missing"),
                        "workflow_id": str(workflow_id),
                    }
                )
            endpoint_id = asset.generation_metadata.get("comfyui_endpoint_id")
            if endpoint_id and str(endpoint_id) not in endpoint_by_id:
                issues.append(
                    {
                        **self._visual_issue(asset, "warning", "visual_endpoint_missing"),
                        "endpoint_id": str(endpoint_id),
                    }
                )

        fail_count = sum(1 for issue in issues if issue["severity"] == "fail")
        warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
        if fail_count:
            severity = QualitySeverity.fail
        elif warning_count:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="visual_media_integrity",
            severity=severity,
            status=severity.value,
            score=0.0 if fail_count else 1.0,
            details={
                "language": transcript.language,
                "checked_visual_asset_count": len(assets),
                "skipped_optional_broll_asset_count": len(
                    skipped_optional_broll_asset_ids
                ),
                "skipped_optional_broll_asset_ids": skipped_optional_broll_asset_ids,
                "completed_visual_asset_count": len(completed_assets),
                "stored_visual_asset_count": sum(
                    1 for asset in completed_assets if asset.storage_uri
                ),
                "probed_visual_asset_count": probed_count,
                "pixel_analyzed_visual_asset_count": pixel_analyzed_count,
                "pixel_warning_visual_asset_count": pixel_warning_count,
                "video_probe_checked_visual_asset_count": video_probe_checked_count,
                "video_probe_warning_visual_asset_count": video_probe_warning_count,
                "video_probe_invalid_visual_asset_count": video_probe_invalid_count,
                "video_probe_missing_frame_count_visual_asset_count": (
                    video_probe_missing_frame_count
                ),
                "video_probe_estimated_frame_count_visual_asset_count": (
                    video_probe_estimated_frame_count
                ),
                "render_suitable_visual_asset_count": sum(
                    1 for asset in assets if self._is_render_suitable_visual_asset(asset)
                ),
                "checked_citation_card_asset_count": sum(
                    1 for asset in assets if self._is_citation_overlay_asset(asset)
                ),
                "render_suitable_citation_card_asset_count": sum(
                    1
                    for asset in assets
                    if self._is_citation_overlay_asset(asset)
                    and self._is_render_suitable_visual_asset(asset)
                ),
                "dimension_checked_visual_asset_count": dimension_checked_count,
                "dimension_mismatch_visual_asset_count": dimension_mismatch_count,
                "fps_checked_visual_asset_count": fps_checked_count,
                "duration_checked_visual_asset_count": duration_checked_count,
                "duration_mismatch_visual_asset_count": duration_mismatch_count,
                "lip_sync_ready_visual_asset_count": lip_sync_ready_count,
                "lip_sync_missing_visual_asset_count": lip_sync_missing_count,
                "lip_sync_measured_visual_asset_count": lip_sync_measured_count,
                "max_lip_sync_offset_ms": max_lip_sync_offset_ms,
                "average_lip_sync_offset_ms": (
                    round(lip_sync_offset_sum_ms / lip_sync_measured_count, 2)
                    if lip_sync_measured_count
                    else None
                ),
                "style_metadata_checked_visual_asset_count": style_checked_count,
                "style_metadata_missing_visual_asset_count": style_missing_count,
                "style_consistency_checked_visual_asset_count": (
                    style_consistency_checked_count
                ),
                "style_consistency_warning_visual_asset_count": (
                    style_consistency_warning_count
                ),
                "identity_consistency_checked_visual_asset_count": (
                    identity_consistency_checked_count
                ),
                "identity_consistency_warning_visual_asset_count": (
                    identity_consistency_warning_count
                ),
                "fallback_visual_asset_count": sum(
                    1
                    for asset in completed_assets
                    if asset.generation_metadata.get("fallback_visual") is True
                ),
                "placeholder_visual_asset_count": sum(
                    1
                    for asset in completed_assets
                    if asset.generation_metadata.get("mock_visual_placeholder") is True
                ),
                "issue_count": len(issues),
                "failure_count": fail_count,
                "warning_count": warning_count,
                "issues": issues,
                "selection": self._selection_details(request),
            },
        )

    def _visual_generation_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        request: VisualGenerationRequest | VisualResultSyncRequest | VisualCancellationRequest,
    ) -> QualityResult:
        assets = self._target_visual_assets(episode, transcript, request)
        issues: list[dict] = []
        for asset in assets:
            if asset.status == "failed":
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "visual_generation_failed",
                        "asset_id": str(asset.id),
                        "source_entity_id": asset.source_entity_id,
                    }
                )
            if asset.status == "completed":
                if not asset.storage_uri:
                    issues.append(
                        {
                            "severity": "fail",
                            "issue": "completed_visual_missing_storage",
                            "asset_id": str(asset.id),
                        }
                    )
                if not asset.checksum:
                    issues.append(
                        {
                            "severity": "warning",
                            "issue": "completed_visual_missing_checksum",
                            "asset_id": str(asset.id),
                        }
                    )
                if asset.generation_metadata.get("render_ready") is False:
                    issues.append(
                        {
                            "severity": "warning",
                            "issue": "visual_placeholder_not_render_ready",
                            "asset_id": str(asset.id),
                        }
                    )
                if asset.generation_metadata.get("fallback_visual") is True:
                    issues.append(
                        {
                            "severity": "warning",
                            "issue": "visual_fallback_used",
                            "asset_id": str(asset.id),
                            "fallback_kind": asset.generation_metadata.get("fallback_kind"),
                            "fallback_reason": asset.generation_metadata.get(
                                "fallback_reason"
                            ),
                        }
                    )
                probe = asset.generation_metadata.get("media_probe")
                if isinstance(probe, dict):
                    for warning in probe.get("probe_warnings", []):
                        issues.append(
                            {
                                "severity": "warning",
                                "issue": "visual_media_probe_warning",
                                "asset_id": str(asset.id),
                                "warning": warning,
                                "probe_tool": probe.get("probe_tool"),
                            }
                        )
                    if self._requires_video_probe(asset):
                        issues.extend(self._video_probe_issues(asset, probe))
                    if (
                        asset.asset_type
                        in {
                            AssetType.image,
                            AssetType.broll,
                            AssetType.thumbnail,
                            AssetType.video,
                            AssetType.reaction_loop,
                            AssetType.studio_scene,
                        }
                        and (probe.get("width") is None or probe.get("height") is None)
                        and asset.generation_metadata.get("mock_visual_placeholder") is not True
                    ):
                        issues.append(
                            {
                                "severity": "warning",
                                "issue": "visual_media_probe_missing_dimensions",
                                "asset_id": str(asset.id),
                            }
                        )
                if (
                    asset.generation_metadata.get("render_ready") is not False
                    and not self._is_render_suitable_visual_asset(asset)
                ):
                    issues.append(
                        {
                            "severity": "warning",
                            "issue": "visual_asset_not_render_suitable",
                            "asset_id": str(asset.id),
                        }
                    )
        fail_count = sum(1 for issue in issues if issue["severity"] == "fail")
        warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
        if fail_count:
            severity = QualitySeverity.fail
        elif warning_count:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="visual_generation_completeness",
            severity=severity,
            status=severity.value,
            score=0.0 if fail_count else 1.0,
            details={
                "language": transcript.language,
                "checked_visual_asset_count": len(assets),
                "completed_visual_asset_count": sum(
                    1 for asset in assets if asset.status == "completed"
                ),
                "submitted_visual_asset_count": sum(
                    1 for asset in assets if asset.status in {"submitted", "running"}
                ),
                "failed_visual_asset_count": sum(1 for asset in assets if asset.status == "failed"),
                "stored_visual_asset_count": sum(
                    1 for asset in assets if asset.status == "completed" and asset.storage_uri
                ),
                "probed_visual_asset_count": sum(
                    1
                    for asset in assets
                    if asset.status == "completed"
                    and isinstance(asset.generation_metadata.get("media_probe"), dict)
                ),
                "render_ready_visual_asset_count": sum(
                    1
                    for asset in assets
                    if asset.status == "completed"
                    and asset.generation_metadata.get("render_ready") is not False
                ),
                "render_suitable_visual_asset_count": sum(
                    1 for asset in assets if self._is_render_suitable_visual_asset(asset)
                ),
                "video_probe_checked_visual_asset_count": sum(
                    1
                    for asset in assets
                    if asset.status == "completed"
                    and self._requires_video_probe(asset)
                    and isinstance(asset.generation_metadata.get("media_probe"), dict)
                ),
                "video_probe_warning_visual_asset_count": sum(
                    1
                    for asset in assets
                    if asset.status == "completed"
                    and self._requires_video_probe(asset)
                    and isinstance(asset.generation_metadata.get("media_probe"), dict)
                    and bool(
                        self._video_probe_issues(
                            asset,
                            asset.generation_metadata["media_probe"],
                        )
                    )
                ),
                "video_probe_invalid_visual_asset_count": sum(
                    1
                    for asset in assets
                    if asset.status == "completed"
                    and self._requires_video_probe(asset)
                    and isinstance(asset.generation_metadata.get("media_probe"), dict)
                    and isinstance(
                        asset.generation_metadata["media_probe"].get("video_analysis"),
                        dict,
                    )
                    and bool(
                        asset.generation_metadata["media_probe"]["video_analysis"].get(
                            "critical_warnings"
                        )
                    )
                ),
                "citation_card_asset_count": sum(
                    1 for asset in assets if self._is_citation_overlay_asset(asset)
                ),
                "completed_citation_card_asset_count": sum(
                    1
                    for asset in assets
                    if self._is_citation_overlay_asset(asset)
                    and asset.status == "completed"
                ),
                "fallback_visual_asset_count": sum(
                    1
                    for asset in assets
                    if asset.status == "completed"
                    and asset.generation_metadata.get("fallback_visual") is True
                ),
                "issue_count": len(issues),
                "failure_count": fail_count,
                "warning_count": warning_count,
                "issues": issues,
                "selection": self._selection_details(request),
            },
        )

    def _is_render_suitable_visual_asset(self, asset: Asset) -> bool:
        if asset.status != "completed" or not asset.storage_uri:
            return False
        if asset.generation_metadata.get("render_ready") is False:
            return False
        if not asset.width or not asset.height:
            return False
        mime_type = asset.mime_type or ""
        if mime_type in {"image/png", "image/jpeg", "image/svg+xml"}:
            return True
        if mime_type.startswith("video/"):
            return bool(asset.duration_ms and asset.fps)
        return False

    def _visual_qc_expectations(
        self,
        asset: Asset,
        episode: Episode,
        workflow_by_id: dict[str, ComfyUiWorkflow],
    ) -> tuple[int, int, float]:
        workflow_id = asset.generation_metadata.get("comfyui_workflow_id")
        workflow = workflow_by_id.get(str(workflow_id)) if workflow_id else None
        if self._is_primary_turn_video(asset):
            if self._is_b1_managed_media_asset(asset):
                workflow = self._workflow_for_asset(asset, workflow_by_id)
            if (
                workflow is not None
                and self._is_b1_managed_media_asset(asset)
                and self._is_b1_audio_driven_lipsync_workflow(workflow)
            ):
                return (
                    int(workflow.default_parameters.get("b1_lipsync_width") or 512),
                    int(workflow.default_parameters.get("b1_lipsync_height") or 512),
                    float(workflow.default_parameters.get("b1_lipsync_fps") or 12),
                )
        if asset.generation_metadata.get("visual_role") == "studio_panel_keyframe":
            if workflow is None:
                return (
                    int(episode.definition.media.width),
                    int(episode.definition.media.height),
                    0.0,
                )
            return (
                int(workflow.default_parameters.get("b1_media_width") or 512),
                int(workflow.default_parameters.get("b1_media_height") or 288),
                0.0,
            )
        # The workflow owns generation geometry.  Episode output geometry is a
        # compositor target and need not match source clips or reusable studio
        # plates.  Comparing source assets to the final canvas produced false
        # QC warnings for otherwise valid 1024x576/12 fps media.
        parameters = workflow.default_parameters if workflow is not None else {}
        return (
            int(parameters.get("width") or episode.definition.media.width),
            int(parameters.get("height") or episode.definition.media.height),
            float(parameters.get("fps") or episode.definition.media.fps),
        )

    def _is_citation_overlay_asset(self, asset: Asset) -> bool:
        return (
            asset.asset_type == AssetType.citation_card
            and asset.generation_metadata.get("visual_role") == "citation_overlay"
        )

    def _visual_issue(self, asset: Asset, severity: str, issue: str) -> dict:
        return {
            "severity": severity,
            "issue": issue,
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type.value,
            "source_entity_type": asset.source_entity_type,
            "source_entity_id": asset.source_entity_id,
            "visual_role": asset.generation_metadata.get("visual_role"),
        }

    def _video_probe_issues(self, asset: Asset, probe: dict) -> list[dict]:
        video_analysis = probe.get("video_analysis")
        if not isinstance(video_analysis, dict):
            return [
                {
                    **self._visual_issue(
                        asset,
                        "warning",
                        "visual_video_probe_analysis_missing",
                    ),
                    "probe_tool": probe.get("probe_tool"),
                }
            ]
        warning_map = {
            "video probe missing video stream": "visual_video_probe_missing_stream",
            "video probe missing codec": "visual_video_probe_missing_codec",
            "video probe missing pixel format": "visual_video_probe_missing_pixel_format",
            "video probe missing dimensions": "visual_video_probe_missing_dimensions",
            "video probe missing duration": "visual_video_probe_missing_duration",
            "video probe missing fps": "visual_video_probe_missing_fps",
            "video probe missing frame count": "visual_video_probe_missing_frame_count",
            "video probe missing exact frame count": (
                "visual_video_probe_frame_count_estimated"
            ),
            "video probe nonpositive frame count": (
                "visual_video_probe_nonpositive_frame_count"
            ),
            "video probe frame count duration mismatch": (
                "visual_video_probe_frame_count_duration_mismatch"
            ),
        }
        probe_warnings = [
            warning
            for warning in probe.get("probe_warnings", [])
            if isinstance(warning, str)
        ]
        issues = []
        for warning in probe_warnings:
            issue_name = warning_map.get(warning)
            if issue_name is None:
                continue
            issues.append(
                {
                    **self._visual_issue(asset, "warning", issue_name),
                    "warning": warning,
                    "probe_tool": probe.get("probe_tool"),
                    "codec_name": video_analysis.get("codec_name"),
                    "pixel_format": video_analysis.get("pixel_format"),
                    "frame_count_source": video_analysis.get("frame_count_source"),
                    "estimated_frame_count": video_analysis.get("estimated_frame_count"),
                }
            )
        return issues

    def _is_video_like_asset(self, asset: Asset) -> bool:
        mime_type = asset.mime_type or ""
        return (
            asset.asset_type
            in {AssetType.video, AssetType.reaction_loop, AssetType.studio_scene}
            or mime_type.startswith("video/")
        )

    def _requires_video_probe(self, asset: Asset) -> bool:
        return (asset.mime_type or "").startswith("video/")

    def _is_primary_turn_video(self, asset: Asset) -> bool:
        return (
            asset.source_entity_type == "transcript_turn"
            and asset.generation_metadata.get("visual_role") == "video_primary"
        )

    def _is_character_visual_asset(self, asset: Asset) -> bool:
        return asset.generation_metadata.get("visual_role") in {
            "video_primary",
            "reaction_loop",
        }

    def _has_visual_style_metadata(self, asset: Asset) -> bool:
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        return bool(
            asset.generation_metadata.get("visual_profile_id")
            and asset.generation_metadata.get("character_name")
            and isinstance(prompt_inputs, dict)
            and prompt_inputs.get("style_prompt")
        )

    def _visual_lip_sync_measurement(
        self,
        asset: Asset,
        audio_asset: Asset | None,
    ) -> dict | None:
        if audio_asset is None or not asset.duration_ms:
            return None
        metadata = asset.generation_metadata
        audio_duration_ms = self._optional_int(metadata.get("audio_duration_ms"))
        if audio_duration_ms is None:
            audio_duration_ms = self._optional_int(audio_asset.duration_ms)
        if audio_duration_ms is None or audio_duration_ms <= 0:
            return None
        provider_offset = self._optional_int(metadata.get("lip_sync_offset_ms"))
        if provider_offset is None:
            provider_offset = self._optional_int(
                metadata.get("provider_lip_sync_offset_ms")
            )
        duration_offset = int(asset.duration_ms) - audio_duration_ms
        offset_ms = provider_offset if provider_offset is not None else duration_offset
        return {
            "offset_ms": offset_ms,
            "absolute_offset_ms": abs(offset_ms),
            "duration_offset_ms": duration_offset,
            "provider_offset_ms": provider_offset,
            "audio_duration_ms": audio_duration_ms,
            "visual_duration_ms": int(asset.duration_ms),
            "measurement_source": (
                "provider_lip_sync_offset"
                if provider_offset is not None
                else "duration_alignment"
            ),
        }

    def _visual_character_consistency(
        self,
        asset: Asset,
    ) -> dict | None:
        expected_character_name = asset.generation_metadata.get("expected_character_name")
        expected_style_prompt = asset.generation_metadata.get("expected_style_prompt")
        if not isinstance(expected_character_name, str) or not expected_character_name:
            return None
        if not isinstance(expected_style_prompt, str) or not expected_style_prompt:
            return None
        profile_id = asset.generation_metadata.get("visual_profile_id")
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        prompt_style = (
            prompt_inputs.get("style_prompt") if isinstance(prompt_inputs, dict) else None
        )
        character_name = asset.generation_metadata.get("character_name")
        identity_score = 1.0 if character_name == expected_character_name else 0.0
        style_score = 1.0 if prompt_style == expected_style_prompt else 0.0
        return {
            "visual_profile_id": profile_id,
            "identity_score": identity_score,
            "style_score": style_score,
            "character_name": character_name,
            "expected_character_name": expected_character_name,
            "style_prompt": prompt_style,
            "expected_style_prompt": expected_style_prompt,
        }

    def _audio_asset_for_visual(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        asset: Asset,
    ) -> Asset | None:
        return next(
            (
                candidate
                for candidate in episode.assets
                if candidate.asset_type == AssetType.audio
                and candidate.language == transcript.language
                and candidate.source_entity_type == "transcript_turn"
                and candidate.source_entity_id == asset.source_entity_id
                and candidate.status == "completed"
            ),
            None,
        )

    def _has_active_visual_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        turn_id: str,
        visual_role: str,
    ) -> bool:
        return (
            self._active_visual_asset(episode, transcript, turn_id, visual_role) is not None
        )

    def _active_visual_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        turn_id: str,
        visual_role: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.language == transcript.language
                and asset.source_entity_type == "transcript_turn"
                and asset.source_entity_id == turn_id
                and asset.status != "replaced"
                and asset.generation_metadata.get("visual_role") == visual_role
            ),
            None,
        )

    def _has_active_reusable_visual_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        source_entity_type: str,
        source_entity_id: str,
        visual_role: str,
    ) -> bool:
        return (
            self._active_reusable_visual_asset(
                episode,
                transcript,
                source_entity_type,
                source_entity_id,
                visual_role,
            )
            is not None
        )

    def _active_reusable_visual_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        source_entity_type: str,
        source_entity_id: str,
        visual_role: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.language == transcript.language
                and asset.source_entity_type == source_entity_type
                and asset.source_entity_id == source_entity_id
                and asset.status != "replaced"
                and asset.generation_metadata.get("visual_role") == visual_role
            ),
            None,
        )

    def _workflow_by_type(
        self,
        workflow_by_id: dict[str, ComfyUiWorkflow],
        workflow_type: str,
    ) -> ComfyUiWorkflow | None:
        return next(
            (
                workflow
                for workflow in workflow_by_id.values()
                if workflow.workflow_type == workflow_type and workflow.enabled
            ),
            None,
        )

    def _primary_shot_type(self, turn_index: int, speaker_participant_id: str) -> str:
        if turn_index == 1 or speaker_participant_id == "host":
            return "talking_head"
        return "talking_head"

    def _turn_audio_duration_ms(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        turn_id: str,
    ) -> int | None:
        for asset in episode.assets:
            if (
                asset.asset_type == AssetType.audio
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_turn"
                and asset.source_entity_id == turn_id
                and asset.status == "completed"
            ):
                return asset.duration_ms
        return None

    def _estimate_visual_duration_ms(self, text: str) -> int:
        word_count = max(1, len(text.split()))
        return max(1000, int((word_count / self.settings.words_per_second) * 1000))

    def _workflow_for_asset(
        self,
        asset: Asset,
        workflow_by_id: dict[str, ComfyUiWorkflow],
    ) -> ComfyUiWorkflow:
        # Older persisted visual profiles incorrectly used the reaction workflow for
        # their primary talking-head shots. Keep those records renderable through the
        # audio-driven B1 path while the profile configuration is migrated.
        if (
            asset.generation_metadata.get("visual_role") == "video_primary"
            and asset.generation_metadata.get("shot_type") == "talking_head"
        ):
            talking_head = workflow_by_id.get("workflow-talking-head-v1")
            if talking_head is not None and talking_head.enabled:
                return talking_head
        workflow_id = asset.generation_metadata.get("comfyui_workflow_id")
        workflow = workflow_by_id.get(str(workflow_id)) if workflow_id else None
        if workflow is None or not workflow.enabled:
            raise ValueError(f"comfyui workflow {workflow_id} is not available")
        return workflow

    def _result_storage_uri(self, payload: dict) -> str | None:
        for key in (
            "storage_uri",
            "video_url",
            "image_url",
            "result_url",
            "media_url",
            "download_url",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                for key in ("url", "download_url", "media_url", "source_url"):
                    value = artifact.get(key)
                    if isinstance(value, str) and value:
                        return value
        return None

    def _media_bytes_from_payload(self, payload: dict) -> bytes | None:
        for key in ("video_base64", "image_base64", "media_base64", "result_base64"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                return base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                return None
        return None

    def _absolute_result_uri(
        self,
        endpoint: ComfyUiEndpoint,
        payload: dict,
        result_uri: str | None,
    ) -> str | None:
        if not result_uri:
            return None
        parsed = urlparse(result_uri)
        if parsed.scheme in {"http", "https"}:
            return result_uri
        if not result_uri.startswith("/"):
            return result_uri
        api_base = str(endpoint.capabilities.get("remote_nodes_api_base") or "").rstrip("/")
        links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
        if api_base and (
            result_uri.startswith("/artifacts/")
            or result_uri.startswith("/v1/")
            or any(isinstance(value, str) and value.startswith("/v1/") for value in links.values())
        ):
            return f"{api_base}{result_uri}"
        if endpoint.base_url:
            return f"{endpoint.base_url.rstrip('/')}{result_uri}"
        return result_uri

    def _comfyui_view_uri(
        self,
        endpoint: ComfyUiEndpoint,
        payload: dict,
    ) -> str | None:
        output = self._first_comfyui_output(payload)
        if output is None or not endpoint.base_url:
            return None
        filename = output.get("filename")
        if not isinstance(filename, str) or not filename:
            return None
        subfolder = output.get("subfolder", "")
        output_type = output.get("type", "output")
        return (
            f"{endpoint.base_url.rstrip('/')}/view?filename={filename}"
            f"&subfolder={subfolder}&type={output_type}"
        )

    def _first_comfyui_output(self, payload: dict) -> dict | None:
        outputs = payload.get("outputs")
        if isinstance(outputs, dict):
            for node in outputs.values():
                if not isinstance(node, dict):
                    continue
                for key in ("videos", "gifs", "images"):
                    items = node.get(key)
                    if isinstance(items, list) and items:
                        first = items[0]
                        if isinstance(first, dict):
                            return first
        if len(payload) == 1:
            only_value = next(iter(payload.values()))
            if isinstance(only_value, dict):
                return self._first_comfyui_output(only_value)
        return None

    def _normalize_visual_status(self, status: object | None) -> str:
        normalized = str(status or "completed").lower()
        if normalized in {
            "succeeded",
            "success",
            "done",
            "complete",
            "review_required",
        }:
            return "completed"
        if normalized in {"queued", "pending"}:
            return "submitted"
        if normalized in {
            "created",
            "validated",
            "waiting_for_gpu",
            "unloading",
            "verifying_vram",
            "loading",
            "warming",
            "processing",
            "in_progress",
            "saving",
        }:
            return "running"
        if normalized in {"error", "failed", "failure"}:
            return "failed"
        if normalized in {"submitted", "running", "completed", "cancelled"}:
            return normalized
        return "completed"

    def _default_visual_mime_type(self, asset: Asset) -> str:
        if asset.generation_metadata.get("visual_role") == "studio_panel_keyframe":
            return "image/png"
        if asset.asset_type in {AssetType.video, AssetType.reaction_loop, AssetType.studio_scene}:
            return "video/mp4"
        if asset.asset_type in {AssetType.broll, AssetType.image, AssetType.thumbnail}:
            return "image/png"
        return "application/octet-stream"

    def _visual_object_key(self, asset: Asset, mime_type: str | None) -> str:
        extension_by_mime = {
            "application/vnd.dialecticore.visual-placeholder+json": "json",
            "application/json": "json",
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/svg+xml": "svg",
            "video/mp4": "mp4",
            "video/webm": "webm",
        }
        extension = extension_by_mime.get(mime_type or "", "bin")
        role = asset.generation_metadata.get("visual_role", asset.asset_type.value)
        return f"visual/{asset.episode_id}/{asset.language or 'und'}/{role}/{asset.id}.{extension}"

    def _selection_details(
        self,
        request: (
            VisualCancellationRequest
            | VisualGenerationRequest
            | VisualQualityRequest
            | VisualResultSyncRequest
        ),
    ) -> dict:
        return {
            "asset_ids": [str(asset_id) for asset_id in request.asset_ids or []],
            "transcript_turn_ids": [
                str(turn_id) for turn_id in request.transcript_turn_ids or []
            ],
            "participant_ids": request.participant_ids or [],
            "failed_only": request.failed_only,
            "regenerate": getattr(request, "regenerate", False),
            "include_completed": getattr(request, "include_completed", False),
            "fallback_on_failure": getattr(request, "fallback_on_failure", None),
            "reset_to_planned": getattr(request, "reset_to_planned", None),
        }

    def _optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _optional_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _fps_from_rate(self, value: object) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        numerator, _, denominator = value.partition("/")
        parsed_numerator = self._optional_float(numerator)
        parsed_denominator = self._optional_float(denominator or "1")
        if not parsed_numerator or not parsed_denominator:
            return None
        return round(parsed_numerator / parsed_denominator, 3)
