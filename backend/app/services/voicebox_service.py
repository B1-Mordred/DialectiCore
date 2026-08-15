from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import math
import re
import shutil
import socket
import subprocess
import tempfile
import wave
from array import array
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import UUID, uuid4

import httpx
from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Asset,
    AudioAssetPlanRequest,
    AudioCancellationRequest,
    AudioGenerationRequest,
    AudioQualityRequest,
    AudioResultSyncRequest,
    AuditEvent,
    Episode,
    ParticipantProfile,
    QualityResult,
    TranscriptTurn,
    TranscriptVersion,
    VoiceboxEndpoint,
    VoiceProfile,
)
from app.services.model_gateway import SecretResolver
from app.services.object_storage import AudioProbe, create_object_store
from app.services.redaction import (
    is_sensitive_provider_response_key,
    safe_provider_response_payload,
)


@dataclass(frozen=True)
class TtsResult:
    status: str
    storage_uri: str | None
    mime_type: str | None
    duration_ms: int | None
    checksum: str | None
    metadata: dict
    audio_bytes: bytes | None = None


class VoiceboxService:
    def __init__(
        self,
        settings: Settings,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport
        self.object_store = create_object_store(settings, secret_resolver=self.secret_resolver)
        self.audio_probe = AudioProbe(
            self.object_store,
            target_lufs=settings.audio_loudness_target_lufs,
            true_peak_limit_dbtp=settings.audio_loudness_true_peak_limit_dbtp,
            loudness_range_target_lu=settings.audio_loudness_range_target_lu,
        )

    async def check_endpoint_health(self, endpoint: VoiceboxEndpoint) -> VoiceboxEndpoint:
        if endpoint.adapter_type == "mock":
            return endpoint.model_copy(
                update={
                    "health_status": "healthy",
                    "capabilities": {
                        **endpoint.capabilities,
                        "tts": True,
                        "word_timestamps": True,
                        "phoneme_timestamps": False,
                        "formats": endpoint.capabilities.get("formats", ["audio/wav"]),
                    },
                }
            )
        if not endpoint.base_url:
            return endpoint.model_copy(update={"health_status": "unconfigured"})
        if self._uses_audio_stream_submission(endpoint):
            return await self._check_audio_stream_endpoint_health(endpoint)

        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        base_url = endpoint.base_url.rstrip("/")
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            health_response = await client.get(f"{base_url}/health")
            health_status = "healthy" if health_response.is_success else "unhealthy"
            capabilities = dict(endpoint.capabilities)
            if health_response.is_success:
                try:
                    health_payload = health_response.json()
                except ValueError:
                    health_payload = {}
                if isinstance(health_payload, dict):
                    discovered = safe_provider_response_payload(
                        health_payload.get("capabilities", {})
                    )
                    if isinstance(discovered, dict):
                        capabilities.update(discovered)
            capability_response = await client.get(f"{base_url}/capabilities")
            if capability_response.is_success:
                try:
                    capability_payload = capability_response.json()
                except ValueError:
                    capability_payload = {}
                if isinstance(capability_payload, dict):
                    safe_capability_payload = safe_provider_response_payload(
                        capability_payload
                    )
                    if isinstance(safe_capability_payload, dict):
                        capabilities.update(safe_capability_payload)
            return endpoint.model_copy(
                update={"health_status": health_status, "capabilities": capabilities}
            )

    async def _check_audio_stream_endpoint_health(
        self,
        endpoint: VoiceboxEndpoint,
    ) -> VoiceboxEndpoint:
        capabilities = dict(endpoint.capabilities)
        credential_reference_configured = bool(endpoint.credential_reference)
        credential_reference_resolved = False
        credential_reference_error: str | None = None
        if credential_reference_configured:
            try:
                credential_reference_resolved = (
                    self.secret_resolver.resolve(endpoint.credential_reference) is not None
                )
            except RuntimeError as exc:
                credential_reference_error = str(exc)
        capabilities.update(
            {
                "tts": True,
                "response_mode": "audio_stream",
                "stream_generation_path": self._capability_path(
                    endpoint,
                    "stream_generation_path",
                    "/generate/stream",
                ),
                "accept": self._capability_string(endpoint, "accept", "audio/wav"),
                "credential_reference_configured": credential_reference_configured,
                "credential_reference_resolved": credential_reference_resolved,
            }
        )
        dns_result = self._base_url_dns_resolution(endpoint)
        if dns_result:
            capabilities["base_url_dns"] = dns_result
            capabilities["base_url_dns_resolved"] = bool(dns_result.get("resolved"))
        if credential_reference_error:
            capabilities["credential_reference_error"] = credential_reference_error
        else:
            capabilities.pop("credential_reference_error", None)
        previous_bootstrap = endpoint.capabilities.get("ca_cert_bootstrap")
        bootstrap_result: dict = {}
        try:
            bootstrap_result = await self._probe_ca_cert_bootstrap(endpoint)
        except httpx.HTTPError as exc:
            if isinstance(previous_bootstrap, dict) and previous_bootstrap.get("stored"):
                bootstrap_result = dict(previous_bootstrap)
                bootstrap_result["probe_error"] = type(exc).__name__
            else:
                capabilities["ca_cert_bootstrap_probe_error"] = type(exc).__name__
        else:
            capabilities.pop("ca_cert_bootstrap_probe_error", None)
        if bootstrap_result:
            if isinstance(previous_bootstrap, dict) and previous_bootstrap.get("stored"):
                bootstrap_result["stored"] = True
            capabilities["ca_cert_bootstrap"] = bootstrap_result

        tls_path = self._capability_string(endpoint, "tls_ca_cert_path", "")
        tls_cert_available = not tls_path or Path(tls_path).is_file()
        capabilities["tls_ca_cert_available"] = tls_cert_available
        bootstrap_required = bool(
            self._capability_string(endpoint, "ca_cert_bootstrap_url", "")
        )
        bootstrap_healthy = (
            not bootstrap_required
            or (bool(bootstrap_result.get("reachable")) or bool(bootstrap_result.get("stored")))
            and bootstrap_result.get("sha256_matches") is not False
        )
        credential_required = endpoint.capabilities.get("credential_required") is not False
        credential_ready = (
            (credential_reference_resolved if credential_required else True)
            if endpoint.adapter_type == "b1_voice_stream"
            else True
        )
        dns_ready = not bool(capabilities.get("require_base_url_dns_resolution")) or bool(
            capabilities.get("base_url_dns_resolved")
        )
        canary_ready = True
        if capabilities.get("generation_canary_enabled") is True:
            canary_result = await self._probe_audio_stream_generation_canary(
                endpoint.model_copy(update={"capabilities": capabilities})
            )
            capabilities["generation_canary"] = canary_result
            # A B1 scheduler lease held by another media job proves the endpoint is
            # reachable. Real TTS submissions retry that transient admission state.
            canary_ready = canary_result.get("status") in {"pass", "busy"}
        health_status = (
            "healthy"
            if (
                credential_ready
                and tls_cert_available
                and bootstrap_healthy
                and dns_ready
                and canary_ready
            )
            else "unhealthy"
        )
        return endpoint.model_copy(
            update={"health_status": health_status, "capabilities": capabilities}
        )

    async def list_b1_voice_profiles(self, endpoint: VoiceboxEndpoint) -> list[dict]:
        """Return the public profile inventory for a native B1 Voicebox endpoint."""
        if endpoint.adapter_type != "b1_voice_stream" or not endpoint.base_url:
            raise ValueError("voice profile inventory requires a native B1 Voicebox endpoint")
        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.get(f"{endpoint.base_url.rstrip('/')}/profiles")
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("B1 voice profile inventory returned an invalid response")
        profiles: list[dict] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            profile_id = item.get("id")
            name = item.get("name")
            language = item.get("language")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (profile_id, name, language)
            ):
                continue
            profiles.append(
                {
                    "id": profile_id.strip(),
                    "name": name.strip(),
                    "description": str(item.get("description") or "").strip(),
                    "language": language.strip(),
                    "engine": str(item.get("default_engine") or "chatterbox").strip(),
                }
            )
        return profiles

    async def _probe_audio_stream_generation_canary(
        self,
        endpoint: VoiceboxEndpoint,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        profile_id = self._capability_string(endpoint, "generation_canary_profile_id", "")
        if not profile_id:
            return {
                "status": "skipped",
                "checked_at": now,
                "reason": "generation_canary_profile_id is not configured",
            }
        base_url = (endpoint.base_url or "").rstrip("/")
        if not base_url:
            return {
                "status": "fail",
                "checked_at": now,
                "reason": "base_url is not configured",
            }
        path = self._capability_path(endpoint, "stream_generation_path", "/generate/stream")
        accept = self._capability_string(endpoint, "accept", "audio/wav")
        engine = self._capability_string(endpoint, "generation_canary_engine", "") or (
            self._capability_string(endpoint, "voice_profile_engine", "")
            or self._capability_string(endpoint, "default_engine", "chatterbox")
        )
        text = self._capability_string(
            endpoint,
            "generation_canary_text",
            "Guten Tag. DialectiCore prueft die Stimme.",
        )
        normalize = bool(endpoint.capabilities.get("normalize_default", False))
        effects_chain = endpoint.capabilities.get("effects_chain_default")
        if not isinstance(effects_chain, list):
            effects_chain = []
        headers = {"accept": accept, "content-type": "application/json"}
        try:
            token = self.secret_resolver.resolve(endpoint.credential_reference)
        except RuntimeError as exc:
            return {
                "status": "fail",
                "checked_at": now,
                "profile_id": profile_id,
                "engine": engine,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
        if token:
            headers["authorization"] = f"Bearer {token}"
        payload = {
            "profile_id": profile_id,
            "text": text,
            "language": self._capability_string(endpoint, "generation_canary_language", "de"),
            "engine": engine,
            "normalize": normalize,
            "effects_chain": effects_chain,
        }
        timeout_seconds = float(
            endpoint.capabilities.get(
                "generation_canary_timeout_seconds",
                min(endpoint.default_timeout_seconds, 30),
            )
        )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(timeout_seconds),
                headers=headers,
                verify=self._endpoint_verify(endpoint),
            ) as client:
                response, attempts = await self._post_audio_stream_with_retry(
                    client=client,
                    endpoint=endpoint,
                    url=f"{base_url}{path}",
                    payload=payload,
                )
        except httpx.HTTPError as exc:
            return {
                "status": "fail",
                "checked_at": now,
                "profile_id": profile_id,
                "engine": engine,
                "error_type": type(exc).__name__,
            }
        content_type = response.headers.get("content-type", "")
        audio_bytes = response.content or b""
        riff_wave = audio_bytes[:12].startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE"
        scheduler_busy = self._is_b1_scheduler_busy_response(response)
        return {
            "status": (
                "pass"
                if response.is_success and riff_wave
                else "busy"
                if scheduler_busy
                else "fail"
            ),
            "checked_at": now,
            "profile_id": profile_id,
            "engine": engine,
            "status_code": response.status_code,
            "content_type": content_type,
            "bytes": len(audio_bytes),
            "riff_wave": riff_wave,
            "text_chars": len(text),
            "attempts": attempts,
        }

    async def _probe_ca_cert_bootstrap(self, endpoint: VoiceboxEndpoint) -> dict:
        bootstrap_url = self._capability_string(endpoint, "ca_cert_bootstrap_url", "")
        if not bootstrap_url:
            return {}
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers={"accept": "application/octet-stream,*/*;q=0.5"},
            verify=False,
        ) as client:
            response = await client.get(bootstrap_url)
            response.raise_for_status()
        observed_sha256 = hashlib.sha256(response.content).hexdigest()
        expected_sha256 = self._capability_string(endpoint, "ca_cert_sha256", "")
        header_sha256 = response.headers.get("x-b1-sha256")
        return {
            "reachable": True,
            "status_code": response.status_code,
            "content_length": len(response.content),
            "sha256": observed_sha256,
            "expected_sha256": expected_sha256 or None,
            "header_sha256": header_sha256,
            "sha256_matches": (
                observed_sha256 == expected_sha256 if expected_sha256 else None
            ),
        }

    async def bootstrap_ca_certificate(self, endpoint: VoiceboxEndpoint) -> VoiceboxEndpoint:
        bootstrap_url = self._capability_string(endpoint, "ca_cert_bootstrap_url", "")
        if not bootstrap_url:
            raise ValueError("voicebox endpoint has no CA bootstrap URL")
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers={"accept": "application/octet-stream,*/*;q=0.5"},
            verify=False,
        ) as client:
            response = await client.get(bootstrap_url)
            response.raise_for_status()

        certificate = response.content
        if not certificate:
            raise ValueError("CA bootstrap response was empty")
        observed_sha256 = hashlib.sha256(certificate).hexdigest()
        expected_sha256 = self._capability_string(endpoint, "ca_cert_sha256", "")
        if expected_sha256 and observed_sha256 != expected_sha256:
            raise ValueError("CA bootstrap SHA-256 did not match expected value")

        certificate_path = self._ca_certificate_storage_path(endpoint)
        certificate_path.parent.mkdir(parents=True, exist_ok=True)
        certificate_path.write_bytes(certificate)
        capabilities = dict(endpoint.capabilities)
        capabilities.update(
            {
                "tls_ca_cert_path": str(certificate_path),
                "ca_cert_bootstrap": {
                    "reachable": True,
                    "status_code": response.status_code,
                    "content_length": len(certificate),
                    "sha256": observed_sha256,
                    "expected_sha256": expected_sha256 or None,
                    "header_sha256": response.headers.get("x-b1-sha256"),
                    "sha256_matches": (
                        observed_sha256 == expected_sha256 if expected_sha256 else None
                    ),
                    "stored": True,
                },
            }
        )
        return endpoint.model_copy(update={"capabilities": capabilities})

    def _ca_certificate_storage_path(self, endpoint: VoiceboxEndpoint) -> Path:
        configured_path = self._capability_string(endpoint, "tls_ca_cert_path", "")
        certificate_dir = Path(self.settings.runtime_state_path) / "certificates"
        if configured_path:
            filename = Path(configured_path).name
            if filename and filename not in {".", ".."}:
                return certificate_dir / filename
        safe_endpoint_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in endpoint.id
        ).strip("-")
        return certificate_dir / f"{safe_endpoint_id or 'voicebox'}-ca.crt"

    def plan_audio_assets(
        self,
        episode: Episode,
        request: AudioAssetPlanRequest,
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            raise ValueError("target transcript has no playable turns")

        episode.status = EpisodeStatus.generating_audio
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.generating_audio.value},
            )
        )

        created_count = 0
        for turn in playable_turns:
            existing = self._existing_audio_asset(
                episode,
                language=transcript.language,
                source_entity_id=str(turn.id),
            )
            if existing and not request.regenerate:
                continue
            participant = self._participant_by_id(episode, turn.speaker_participant_id)
            episode.assets.append(
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.audio,
                    language=transcript.language,
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    mime_type="audio/wav",
                    duration_ms=self._estimate_duration_ms(turn.text),
                    generation_metadata={
                        "adapter": "voicebox",
                        "status": "planned",
                        "transcript_version_id": str(transcript.id),
                        "transcript_type": transcript.type.value,
                        "localization": transcript.localization_metadata,
                        "speaker_participant_id": turn.speaker_participant_id,
                        "voice_profile_id": participant.voice_profile_id,
                        "words_per_second": self.settings.words_per_second,
                    },
                    status="planned",
                )
            )
            created_count += 1

        qc = self._audio_plan_qc(episode, transcript)
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="audio.assets.planned",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "created_count": created_count,
                    "planned_count": qc.details["planned_audio_asset_count"],
                    "required_count": qc.details["required_audio_asset_count"],
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
        if created_count == 0 and not request.regenerate:
            raise ValueError("audio assets already planned for target transcript")
        return episode

    async def generate_audio_assets(
        self,
        episode: Episode,
        request: AudioGenerationRequest,
        voicebox_endpoints: list[VoiceboxEndpoint],
        voice_profiles: list[VoiceProfile],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        planned_assets = self._target_audio_assets(episode, transcript, request)
        if not planned_assets:
            raise ValueError("target transcript has no planned audio assets")

        endpoint_by_id = {endpoint.id: endpoint for endpoint in voicebox_endpoints}
        profile_by_id = {profile.id: profile for profile in voice_profiles}
        turn_by_id = {str(turn.id): turn for turn in transcript.turns}
        self._ensure_stream_endpoint_connectivity(
            episode=episode,
            transcript=transcript,
            assets=planned_assets,
            endpoint_by_id=endpoint_by_id,
            profile_by_id=profile_by_id,
        )

        episode.status = EpisodeStatus.generating_audio
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.generating_audio.value},
            )
        )

        completed_count = 0
        failed_count = 0
        submitted_count = 0
        skipped_count = 0
        for asset in planned_assets:
            if asset.status == "completed" and not request.regenerate:
                skipped_count += 1
                continue
            turn = turn_by_id.get(asset.source_entity_id)
            if turn is None:
                asset.status = "failed"
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "failure": "source transcript turn not found",
                }
                asset.updated_at = datetime.now(UTC)
                failed_count += 1
                continue
            participant = self._participant_by_id(episode, turn.speaker_participant_id)
            voice_profile_id = self._voice_profile_id_for_participant(participant, profile_by_id)
            if voice_profile_id is None:
                raise ValueError(f"participant {participant.id} has no voice profile")
            voice_profile = profile_by_id.get(voice_profile_id)
            if voice_profile is None or not voice_profile.enabled:
                raise ValueError(f"voice profile {voice_profile_id} is not available")
            endpoint = endpoint_by_id.get(voice_profile.voicebox_endpoint_id)
            if endpoint is None or not endpoint.enabled:
                raise ValueError(
                    f"voicebox endpoint {voice_profile.voicebox_endpoint_id} is not available"
                )

            if request.regenerate and self._requires_cancel_before_retry(asset):
                cancelled = await self._cancel_existing_job_before_retry(endpoint, asset)
                if not cancelled:
                    failed_count += 1
                    continue

            asset.status = "submitted"
            asset.updated_at = datetime.now(UTC)
            try:
                result = await self._submit_tts(endpoint, voice_profile, transcript, turn, asset)
            except httpx.HTTPError as exc:
                asset.status = "failed"
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "adapter": "voicebox",
                    "adapter_type": endpoint.adapter_type,
                    "voicebox_endpoint_id": endpoint.id,
                    "voice_profile_id": voice_profile.id,
                    "remote_profile_id": voice_profile.voice_id,
                    "status": "failed",
                    "failure": str(exc),
                    "failure_type": type(exc).__name__,
                    "failed_at": datetime.now(UTC).isoformat(),
                }
                asset.updated_at = datetime.now(UTC)
                failed_count += 1
                continue

            result = await self._materialize_audio_result(
                endpoint,
                transcript,
                asset,
                result,
                voice_profile=voice_profile,
            )
            result = self._with_timing_tracks(endpoint, turn, asset, result)
            asset.status = result.status
            if result.storage_uri is not None:
                asset.storage_uri = result.storage_uri
            if result.mime_type is not None:
                asset.mime_type = result.mime_type
            if result.duration_ms is not None:
                asset.duration_ms = result.duration_ms
            if result.checksum is not None:
                asset.checksum = result.checksum
            previous_attempts = int(asset.generation_metadata.get("generation_attempt_count", 0))
            generation_metadata = {
                **asset.generation_metadata,
                **result.metadata,
                "status": result.status,
                "generation_attempt_count": previous_attempts + 1,
                "completed_at": datetime.now(UTC).isoformat()
                if result.status == "completed"
                else None,
            }
            if result.status == "completed":
                generation_metadata.pop("failure", None)
                generation_metadata.pop("failed_at", None)
            asset.generation_metadata = {
                **generation_metadata,
            }
            asset.updated_at = datetime.now(UTC)
            if result.status == "completed":
                completed_count += 1
            elif result.status in {"failed", "error"}:
                failed_count += 1
            else:
                submitted_count += 1

        timing_update = self._update_discussion_audio_timing(episode, transcript)
        qc = self._audio_generation_qc(episode, transcript)
        episode.quality_results.append(qc)
        media_qc = self._audio_media_qc(
            episode=episode,
            transcript=transcript,
            request=request.model_copy(
                update={
                    "asset_ids": [asset.id for asset in planned_assets],
                    "failed_only": False,
                }
            ),
            voicebox_endpoints=voicebox_endpoints,
            voice_profiles=voice_profiles,
        )
        episode.quality_results.append(media_qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="audio.assets.generated",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                    "submitted_count": submitted_count,
                    "skipped_count": skipped_count,
                    "required_count": qc.details["required_audio_asset_count"],
                    "checked_count": media_qc.details["checked_audio_asset_count"],
                    "media_qc_status": media_qc.status,
                    "actual_duration_turns_updated": timing_update["turns_updated"],
                    "actual_duration_language": timing_update["language"],
                    "selection": self._selection_details(request),
                },
            )
        )
        if request.regenerate or self._is_selective_generation_request(request):
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="audio.assets.regenerated",
                    actor=request.user_id or "system",
                    details={
                        "transcript_version_id": str(transcript.id),
                        "language": transcript.language,
                        "completed_count": completed_count,
                        "failed_count": failed_count,
                        "submitted_count": submitted_count,
                        "skipped_count": skipped_count,
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

    async def _materialize_audio_result(
        self,
        endpoint: VoiceboxEndpoint,
        transcript: TranscriptVersion,
        asset: Asset,
        result: TtsResult,
        *,
        voice_profile: VoiceProfile | None = None,
    ) -> TtsResult:
        if result.status != "completed":
            return result

        metadata = dict(result.metadata)
        if result.audio_bytes:
            mime_type = result.mime_type or "audio/wav"
            extension = self._audio_extension(mime_type)
            key = f"audio/{asset.episode_id}/{transcript.language}/{asset.id}.{extension}"
            payload, postprocess_metadata = self._postprocess_audio_bytes(
                endpoint=endpoint,
                payload=result.audio_bytes,
                mime_type=mime_type,
                extension=extension,
                delivery_rate=self._local_delivery_rate(endpoint, voice_profile),
            )
            if postprocess_metadata.get("delivery_rate_failed") is True:
                return self._delivery_rate_failure(result, metadata, postprocess_metadata)
            stored = self.object_store.put_bytes(key, payload, mime_type)
            probe = self.audio_probe.probe_path(stored.path, fallback_mime_type=mime_type)
            metadata = self._apply_delivery_rate_timing(
                metadata,
                postprocess_metadata,
                duration_ms=probe.duration_ms or result.duration_ms,
                checksum=stored.checksum,
            )
            metadata.update(
                {
                    "storage_backend": stored.backend,
                    "object_storage_bucket": self.object_store.bucket,
                    "object_storage_key": stored.key,
                    "object_storage_path": str(stored.path),
                    "object_size_bytes": stored.size_bytes,
                    "checksum_source": "stored_audio",
                    "media_probe": self._probe_metadata(probe),
                    **postprocess_metadata,
                }
            )
            return replace(
                result,
                storage_uri=stored.uri,
                checksum=stored.checksum,
                duration_ms=probe.duration_ms or result.duration_ms,
                mime_type=probe.mime_type or mime_type,
                metadata=metadata,
            )

        if result.storage_uri and self._is_downloadable_result_uri(endpoint, result.storage_uri):
            try:
                downloaded = await self._download_remote_audio_result(endpoint, result.storage_uri)
            except httpx.HTTPError as exc:
                metadata.update(
                    {
                        "status": "failed",
                        "failure": "remote audio result download failed",
                        "remote_result_uri": result.storage_uri,
                        "last_download_error": str(exc),
                        "download_failed_at": datetime.now(UTC).isoformat(),
                    }
                )
                return replace(result, status="failed", metadata=metadata)

            mime_type = downloaded["content_type"] or result.mime_type or "audio/wav"
            extension = self._audio_extension(mime_type)
            key = f"audio/{asset.episode_id}/{transcript.language}/{asset.id}.{extension}"
            payload, postprocess_metadata = self._postprocess_audio_bytes(
                endpoint=endpoint,
                payload=downloaded["payload"],
                mime_type=mime_type,
                extension=extension,
                delivery_rate=self._local_delivery_rate(endpoint, voice_profile),
            )
            if postprocess_metadata.get("delivery_rate_failed") is True:
                return self._delivery_rate_failure(result, metadata, postprocess_metadata)
            stored = self.object_store.put_bytes(key, payload, mime_type)
            probe = self.audio_probe.probe_path(stored.path, fallback_mime_type=mime_type)
            metadata = self._apply_delivery_rate_timing(
                metadata,
                postprocess_metadata,
                duration_ms=probe.duration_ms or result.duration_ms,
                checksum=stored.checksum,
            )
            metadata.update(
                {
                    "storage_backend": stored.backend,
                    "object_storage_bucket": self.object_store.bucket,
                    "object_storage_key": stored.key,
                    "object_storage_path": str(stored.path),
                    "object_size_bytes": stored.size_bytes,
                    "checksum_source": "stored_audio",
                    "remote_result_uri": result.storage_uri,
                    "remote_result_downloaded": True,
                    "remote_result_downloaded_at": datetime.now(UTC).isoformat(),
                    "media_probe": self._probe_metadata(probe),
                    **postprocess_metadata,
                }
            )
            return replace(
                result,
                storage_uri=stored.uri,
                checksum=stored.checksum,
                duration_ms=probe.duration_ms or result.duration_ms,
                mime_type=probe.mime_type or mime_type,
                metadata=metadata,
            )

        if result.storage_uri and self._is_http_uri(result.storage_uri):
            metadata.update(
                {
                    "remote_result_uri": result.storage_uri,
                    "remote_result_downloaded": False,
                    "remote_result_download_skipped": (
                        "external URL not allowed by endpoint capabilities"
                    ),
                }
            )
            return replace(result, metadata=metadata)

        if result.storage_uri and result.storage_uri.startswith("object://"):
            probe = self.audio_probe.probe_uri(result.storage_uri, result.mime_type)
            metadata["media_probe"] = self._probe_metadata(probe)
            return replace(
                result,
                duration_ms=probe.duration_ms or result.duration_ms,
                mime_type=probe.mime_type or result.mime_type,
                metadata=metadata,
            )
        return result

    async def render_voice_preview(
        self,
        endpoint: VoiceboxEndpoint,
        voice_profile: VoiceProfile,
        text: str,
    ) -> tuple[bytes, str]:
        """Generate a short, non-persistent sample for voice configuration."""
        transcript = TranscriptVersion(
            episode_id=uuid4(),
            type=TranscriptType.broadcast,
            language=voice_profile.language,
            status="approved",
        )
        turn = TranscriptTurn(
            transcript_version_id=transcript.id,
            source_discussion_turn_ids=[],
            speaker_participant_id="voice-preview",
            text=text,
            status="approved",
        )
        asset = Asset(
            episode_id=transcript.episode_id,
            asset_type=AssetType.audio,
            language=voice_profile.language,
            source_entity_type="voice_preview",
            source_entity_id=str(turn.id),
            mime_type="audio/wav",
            status="submitted",
        )
        result = await self._submit_tts(endpoint, voice_profile, transcript, turn, asset)
        if result.status != "completed":
            raise ValueError("voice preview generation did not complete")
        payload = result.audio_bytes
        mime_type = result.mime_type or "audio/wav"
        if payload is None and result.storage_uri and self._is_downloadable_result_uri(
            endpoint, result.storage_uri
        ):
            downloaded = await self._download_remote_audio_result(endpoint, result.storage_uri)
            payload = downloaded["payload"]
            mime_type = downloaded["content_type"] or mime_type
        if not payload:
            raise ValueError("voice preview generation returned no playable audio")
        payload, metadata = self._postprocess_audio_bytes(
            endpoint=endpoint,
            payload=payload,
            mime_type=mime_type,
            extension=self._audio_extension(mime_type),
            delivery_rate=self._local_delivery_rate(endpoint, voice_profile),
        )
        if metadata.get("delivery_rate_failed") is True:
            raise ValueError("requested delivery pace could not be applied to the voice preview")
        return payload, mime_type

    async def verify_spoken_text(
        self,
        endpoint: VoiceboxEndpoint,
        result: TtsResult,
        *,
        expected_text: str,
        language: str,
    ) -> TtsResult:
        """Independently transcribe generated audio before it enters a render timeline."""
        metadata = dict(result.metadata)
        if result.status != "completed" or not result.audio_bytes:
            metadata["transcription_qc"] = {
                "status": "unavailable",
                "reason": "generated_audio_unavailable",
                "passed": False,
            }
            return replace(result, metadata=metadata)

        base_url = self._transcription_base_url(endpoint)
        if not base_url:
            metadata["transcription_qc"] = {
                "status": "unavailable",
                "reason": "transcription_endpoint_not_configured",
                "passed": False,
            }
            return replace(result, metadata=metadata)

        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        model = self._capability_string(endpoint, "transcription_model", "stt-default")
        timeout_seconds = max(
            1.0,
            float(endpoint.capabilities.get("transcription_timeout_seconds") or 120),
        )
        files = {
            "file": (
                "dialogue.wav",
                result.audio_bytes,
                result.mime_type or "audio/wav",
            )
        }
        data = {
            "model": model,
            "language": language,
            "response_format": "verbose_json",
        }
        transcription_path = self._capability_path(
            endpoint,
            "transcription_path",
            "/v1/audio/transcriptions",
        )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(timeout_seconds),
                headers=headers,
                verify=self._endpoint_verify(endpoint),
            ) as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}{transcription_path}",
                    files=files,
                    data=data,
                )
        except httpx.HTTPError as exc:
            metadata["transcription_qc"] = {
                "status": "unavailable",
                "reason": "transcription_request_failed",
                "error_type": type(exc).__name__,
                "passed": False,
                "provider": "b1_stt",
                "model": model,
            }
            return replace(result, metadata=metadata)

        if not response.is_success:
            metadata["transcription_qc"] = {
                "status": "unavailable",
                "reason": f"transcription_http_{response.status_code}",
                "passed": False,
                "provider": "b1_stt",
                "model": model,
            }
            return replace(result, metadata=metadata)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        spoken_text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(spoken_text, str) or not spoken_text.strip():
            metadata["transcription_qc"] = {
                "status": "unavailable",
                "reason": "transcription_response_missing_text",
                "passed": False,
                "provider": "b1_stt",
                "model": model,
            }
            return replace(result, metadata=metadata)

        qc = self._spoken_text_qc(expected_text, spoken_text)
        qc.update(
            {
                "status": "passed" if qc["passed"] else "failed",
                "provider": "b1_stt",
                "model": model,
                "expected_text_sha256": hashlib.sha256(expected_text.encode()).hexdigest(),
                "transcript_sha256": hashlib.sha256(spoken_text.encode()).hexdigest(),
            }
        )
        metadata["transcription_qc"] = qc
        return replace(result, metadata=metadata)

    def _postprocess_audio_bytes(
        self,
        endpoint: VoiceboxEndpoint,
        payload: bytes,
        mime_type: str,
        extension: str,
        delivery_rate: float = 1.0,
    ) -> tuple[bytes, dict]:
        delivery_rate = self._normalised_delivery_rate(delivery_rate)
        requires_delivery_rate = not math.isclose(delivery_rate, 1.0, abs_tol=0.001)
        requires_loudness = bool(endpoint.capabilities.get("postprocess_audio_loudness"))
        if not requires_loudness and not requires_delivery_rate:
            return payload, {
                "audio_postprocess_applied": False,
                "delivery_rate": delivery_rate,
                "delivery_rate_applied": False,
            }
        if mime_type not in {"audio/wav", "audio/x-wav"}:
            if requires_delivery_rate:
                return payload, {
                    "audio_postprocess_applied": False,
                    "audio_postprocess_skipped": "unsupported_mime_type",
                    "delivery_rate": delivery_rate,
                    "delivery_rate_applied": False,
                    "delivery_rate_failed": True,
                }
            return payload, {
                "audio_postprocess_applied": False,
                "audio_postprocess_skipped": "unsupported_mime_type",
                "delivery_rate": delivery_rate,
                "delivery_rate_applied": False,
            }
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            if requires_delivery_rate:
                return payload, {
                    "audio_postprocess_applied": False,
                    "audio_postprocess_skipped": "ffmpeg_unavailable",
                    "delivery_rate": delivery_rate,
                    "delivery_rate_applied": False,
                    "delivery_rate_failed": True,
                }
            return payload, {
                "audio_postprocess_applied": False,
                "audio_postprocess_skipped": "ffmpeg_unavailable",
                "delivery_rate": delivery_rate,
                "delivery_rate_applied": False,
            }

        with tempfile.TemporaryDirectory(prefix="dialecticore-audio-postprocess-") as tmp:
            directory = Path(tmp)
            source_path = directory / f"source.{extension}"
            output_path = directory / f"normalized.{extension}"
            source_path.write_bytes(payload)
            filters: list[str] = []
            if requires_delivery_rate:
                filters.append(f"atempo={delivery_rate:.4f}")
            true_peak_limit = self._audio_postprocess_true_peak_limit(endpoint)
            if requires_loudness:
                filters.extend(
                    [
                        (
                            "loudnorm="
                            f"I={self.settings.audio_loudness_target_lufs}:"
                            f"TP={true_peak_limit}:"
                            f"LRA={self.settings.audio_loudness_range_target_lu}"
                        ),
                        "alimiter=limit=0.90:level=false",
                    ]
                )
            filters.append(
                f"aresample={self._postprocess_audio_sample_rate(endpoint, source_path)}"
            )
            command = [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-af",
                ",".join(filters),
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=int(endpoint.capabilities.get("audio_postprocess_timeout_seconds", 30)),
                )
                normalized = output_path.read_bytes()
            except (subprocess.SubprocessError, OSError) as exc:
                return payload, {
                    "audio_postprocess_applied": False,
                    "audio_postprocess_skipped": "ffmpeg_failed",
                    "audio_postprocess_failure": str(exc),
                    "delivery_rate": delivery_rate,
                    "delivery_rate_applied": False,
                    "delivery_rate_failed": requires_delivery_rate,
                }
        metadata = {
            "audio_postprocess_applied": requires_loudness,
            "audio_postprocess": (
                "ffmpeg_atempo_loudnorm_alimiter"
                if requires_delivery_rate and requires_loudness
                else "ffmpeg_loudnorm_alimiter"
                if requires_loudness
                else "ffmpeg_atempo"
            ),
            "audio_postprocess_source_size_bytes": len(payload),
            "audio_postprocess_size_bytes": len(normalized),
            "delivery_rate": delivery_rate,
            "delivery_rate_applied": requires_delivery_rate,
        }
        if requires_loudness:
            metadata.update(
                {
                    "audio_postprocess_target_lufs": self.settings.audio_loudness_target_lufs,
                    "audio_postprocess_true_peak_limit_dbtp": true_peak_limit,
                }
            )
        return normalized, metadata

    @staticmethod
    def _normalised_delivery_rate(value: float) -> float:
        return min(1.25, max(0.7, float(value)))

    def _local_delivery_rate(
        self,
        endpoint: VoiceboxEndpoint,
        voice_profile: VoiceProfile | None,
    ) -> float:
        if voice_profile is None or endpoint.adapter_type != "b1_voice_stream":
            return 1.0
        return self._normalised_delivery_rate(voice_profile.rate)

    def _delivery_rate_failure(
        self,
        result: TtsResult,
        metadata: dict,
        processing_metadata: dict,
    ) -> TtsResult:
        metadata.update(processing_metadata)
        metadata.update(
            {
                "status": "failed",
                "failure": "requested delivery pace could not be applied to generated audio",
                "failure_type": "delivery_rate_processing_failed",
            }
        )
        return replace(result, status="failed", metadata=metadata)

    def _apply_delivery_rate_timing(
        self,
        metadata: dict,
        processing_metadata: dict,
        *,
        duration_ms: int | None,
        checksum: str,
    ) -> dict:
        if processing_metadata.get("delivery_rate_applied") is not True:
            return metadata
        source_duration_ms = self._timing_duration_ms(metadata)
        if source_duration_ms is None:
            source_duration_ms = self._optional_positive_int(
                processing_metadata.get("duration_ms")
            )
        rate = float(processing_metadata.get("delivery_rate") or 1.0)
        scale = (
            float(duration_ms) / source_duration_ms
            if duration_ms is not None and source_duration_ms and source_duration_ms > 0
            else 1.0 / rate
        )
        for track_key in ("word_timestamps", "phoneme_timestamps", "character_timestamps"):
            track = metadata.get(track_key)
            if isinstance(track, list):
                metadata[track_key] = self._scale_timestamp_track(track, scale)
        for timing_key in ("word_timing", "phoneme_timing"):
            timing = metadata.get(timing_key)
            if isinstance(timing, dict):
                metadata[timing_key] = {
                    **timing,
                    "duration_ms": duration_ms,
                    "delivery_rate_scaled": True,
                }
        b1_timing = metadata.get("b1_timing")
        if isinstance(b1_timing, dict):
            source_checksum = b1_timing.get("audio_sha256")
            metadata["b1_timing"] = {
                **b1_timing,
                "source_audio_sha256": source_checksum,
                "audio_sha256": checksum,
                "checksum_bound": False,
                "timing_transformed_for_delivery_rate": True,
            }
        metadata["delivery_pace"] = {
            "mode": "ffmpeg_atempo_pitch_preserving",
            "rate": rate,
            "source_duration_ms": source_duration_ms,
            "duration_ms": duration_ms,
            "timing_scale": round(scale, 8),
        }
        return metadata

    @staticmethod
    def _timing_duration_ms(metadata: dict) -> int | None:
        for key in ("word_timing", "phoneme_timing"):
            value = metadata.get(key)
            if isinstance(value, dict):
                try:
                    duration_ms = int(value.get("duration_ms"))
                except (TypeError, ValueError):
                    continue
                if duration_ms > 0:
                    return duration_ms
        return None

    @staticmethod
    def _optional_positive_int(value: object) -> int | None:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _scale_timestamp_track(track: list, scale: float) -> list:
        scaled: list = []
        for item in track:
            if not isinstance(item, dict):
                scaled.append(item)
                continue
            transformed = dict(item)
            for key in ("start_ms", "end_ms", "duration_ms"):
                value = transformed.get(key)
                try:
                    transformed[key] = int(round(float(value) * scale))
                except (TypeError, ValueError):
                    continue
            scaled.append(transformed)
        return scaled

    def _audio_postprocess_true_peak_limit(self, endpoint: VoiceboxEndpoint) -> float:
        configured = endpoint.capabilities.get("audio_postprocess_true_peak_limit_dbtp")
        if configured is not None:
            return float(configured)
        return float(self.settings.audio_loudness_true_peak_limit_dbtp) - 1.0

    def _postprocess_audio_sample_rate(
        self,
        endpoint: VoiceboxEndpoint,
        source_path: Path,
    ) -> int:
        configured = endpoint.capabilities.get("sample_rates")
        if not configured:
            configured = endpoint.capabilities.get("expected_sample_rates")
        if isinstance(configured, list) and configured:
            return int(configured[0])
        try:
            with wave.open(str(source_path), "rb") as audio:
                return int(audio.getframerate())
        except (wave.Error, OSError):
            return 48000

    async def _download_remote_audio_result(
        self,
        endpoint: VoiceboxEndpoint,
        result_uri: str,
    ) -> dict:
        headers = {"accept": "audio/*,application/octet-stream;q=0.9,*/*;q=0.5"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token and self._should_send_download_authorization(endpoint, result_uri):
            headers["authorization"] = f"Bearer {token}"
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.get(result_uri)
            response.raise_for_status()
            return {
                "payload": response.content,
                "content_type": response.headers.get("content-type", "").split(";")[0],
            }

    def _is_downloadable_result_uri(self, endpoint: VoiceboxEndpoint, result_uri: str) -> bool:
        if not self._is_http_uri(result_uri):
            return False
        if self._same_origin(endpoint.base_url, result_uri):
            return True
        return bool(endpoint.capabilities.get("allow_external_result_urls"))

    def _should_send_download_authorization(
        self,
        endpoint: VoiceboxEndpoint,
        result_uri: str,
    ) -> bool:
        return self._same_origin(endpoint.base_url, result_uri) or bool(
            endpoint.capabilities.get("result_download_include_authorization")
        )

    def _is_http_uri(self, uri: str) -> bool:
        scheme = urlparse(uri).scheme.lower()
        return scheme in {"http", "https"}

    def _same_origin(self, base_url: str | None, result_uri: str) -> bool:
        if not base_url:
            return False
        base = urlparse(base_url)
        target = urlparse(result_uri)
        return (
            base.scheme.lower() == target.scheme.lower()
            and base.netloc.lower() == target.netloc.lower()
        )

    def _probe_metadata(self, probe) -> dict:
        return {
            "duration_ms": probe.duration_ms,
            "mime_type": probe.mime_type,
            "sample_rate": probe.sample_rate,
            "channels": probe.channels,
            "format_name": probe.format_name,
            "bit_rate": probe.bit_rate,
            "size_bytes": probe.size_bytes,
            "peak_dbfs": probe.peak_dbfs,
            "rms_dbfs": probe.rms_dbfs,
            "loudness_lufs": probe.loudness_lufs,
            "loudness_source": probe.loudness_source,
            "loudness_range_lu": probe.loudness_range_lu,
            "loudness_threshold_lufs": probe.loudness_threshold_lufs,
            "true_peak_dbtp": probe.true_peak_dbtp,
            "loudness_target_lufs": probe.loudness_target_lufs,
            "true_peak_target_dbtp": probe.true_peak_target_dbtp,
            "loudness_range_target_lu": probe.loudness_range_target_lu,
            "loudness_normalization_gain_db": probe.loudness_normalization_gain_db,
            "loudness_target_offset_lu": probe.loudness_target_offset_lu,
            "loudness_normalization_type": probe.loudness_normalization_type,
            "silence_ratio": probe.silence_ratio,
            "clipping_detected": probe.clipping_detected,
            "probe_tool": probe.probe_tool,
            "probe_warnings": probe.probe_warnings,
        }

    def _audio_extension(self, mime_type: str) -> str:
        if mime_type in {"audio/wav", "audio/x-wav"}:
            return "wav"
        if mime_type == "audio/mpeg":
            return "mp3"
        if mime_type == "audio/flac":
            return "flac"
        return "bin"

    def _with_timing_tracks(
        self,
        endpoint: VoiceboxEndpoint,
        turn: TranscriptTurn | None,
        asset: Asset,
        result: TtsResult,
    ) -> TtsResult:
        if result.status != "completed":
            return result
        metadata = dict(result.metadata)
        duration_ms = result.duration_ms or asset.duration_ms
        raw_word_timestamps = metadata.get("word_timestamps")
        word_timing_source = "provider_word_timestamps"
        if (
            not self._valid_word_timestamp_items(raw_word_timestamps, duration_ms)
            and turn is not None
            and duration_ms is not None
            and endpoint.capabilities.get("estimate_word_timestamps_from_text")
        ):
            raw_word_timestamps = self._mock_word_timestamps(turn.text, duration_ms)
            word_timing_source = "estimated_from_transcript_text"
            metadata["word_timestamps"] = raw_word_timestamps
            metadata["word_timing"] = {
                "source": word_timing_source,
                "confidence": 0.35,
                "word_count": len(raw_word_timestamps),
                "duration_ms": duration_ms,
            }
        valid_words = self._valid_word_timestamp_items(raw_word_timestamps, duration_ms)
        if valid_words and not isinstance(metadata.get("word_timing"), dict):
            metadata["word_timing"] = {
                "source": word_timing_source,
                "confidence": self._timestamp_confidence(valid_words, default=0.8),
                "word_count": len(valid_words),
                "duration_ms": duration_ms,
            }
        provider_phonemes = self._normalize_phoneme_timestamps(
            metadata.get("phoneme_timestamps"),
            duration_ms=duration_ms,
        )
        if provider_phonemes:
            phoneme_track = provider_phonemes
            timing_source = str(
                metadata.get("phoneme_timing_source") or "provider_phoneme_timestamps"
            )
            timing_confidence = self._timestamp_confidence(provider_phonemes, default=1.0)
        else:
            phoneme_track = self._estimated_phoneme_timestamps(
                raw_word_timestamps,
                duration_ms=duration_ms,
                source=word_timing_source,
            )
            if phoneme_track and word_timing_source == "estimated_from_transcript_text":
                timing_source = "estimated_from_transcript_text"
                timing_confidence = 0.35
            else:
                timing_source = "estimated_from_word_timestamps" if phoneme_track else "missing"
                timing_confidence = 0.55 if phoneme_track else 0.0

        viseme_track = [
            {
                "viseme": self._viseme_for_phoneme(item["phoneme"]),
                "phoneme": item["phoneme"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "source": item["source"],
            }
            for item in phoneme_track
        ]
        metadata.update(
            {
                "normalized_phoneme_timestamps": phoneme_track,
                "viseme_timestamps": viseme_track,
                "phoneme_timing": {
                    "source": timing_source,
                    "confidence": timing_confidence,
                    "phoneme_count": len(phoneme_track),
                    "viseme_count": len(viseme_track),
                    "duration_ms": duration_ms,
                    "ready_for_lipsync": bool(viseme_track),
                    "ready_for_styled_subtitles": bool(phoneme_track),
                },
            }
        )
        return replace(result, metadata=metadata)

    def _normalize_phoneme_timestamps(
        self,
        raw_phonemes: object,
        duration_ms: int | None,
    ) -> list[dict]:
        if not isinstance(raw_phonemes, list):
            return []
        normalized: list[dict] = []
        for index, item in enumerate(raw_phonemes):
            if not isinstance(item, dict):
                continue
            label = item.get("phoneme") or item.get("label") or item.get("symbol")
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if label is None or start_ms is None or end_ms is None:
                continue
            try:
                start = int(start_ms)
                end = int(end_ms)
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start:
                continue
            if duration_ms is not None and end > duration_ms + 180:
                continue
            normalized.append(
                {
                    "index": len(normalized),
                    "phoneme": str(label).lower(),
                    "start_ms": start,
                    "end_ms": end,
                    "source": str(item.get("source") or "provider"),
                    "word_index": item.get("word_index"),
                    "confidence": float(item.get("confidence", 1.0)),
                    "provider_index": index,
                }
            )
        return normalized

    def _timestamp_confidence(self, timestamps: list[dict], *, default: float) -> float:
        values: list[float] = []
        for item in timestamps:
            try:
                values.append(float(item.get("confidence", default)))
            except (TypeError, ValueError):
                continue
        if not values:
            return default
        return round(sum(values) / len(values), 4)

    def _estimated_phoneme_timestamps(
        self,
        raw_words: object,
        duration_ms: int | None,
        source: str = "provider_word_timestamps",
    ) -> list[dict]:
        words = self._valid_word_timestamp_items(raw_words, duration_ms)
        timing_source = (
            "estimated_from_transcript_text"
            if source == "estimated_from_transcript_text"
            else "estimated_from_word_timestamps"
        )
        confidence = 0.35 if timing_source == "estimated_from_transcript_text" else 0.55
        estimated: list[dict] = []
        for word_index, word in enumerate(words):
            text = str(word["word"])
            labels = self._estimated_phoneme_labels(text)
            if not labels:
                continue
            start = int(word["start_ms"])
            end = int(word["end_ms"])
            span = max(1, end - start)
            step = max(1, span // len(labels))
            for label_index, label in enumerate(labels):
                phoneme_start = start + label_index * step
                phoneme_end = (
                    end
                    if label_index == len(labels) - 1
                    else min(end, phoneme_start + step)
                )
                estimated.append(
                    {
                        "index": len(estimated),
                        "phoneme": label,
                        "start_ms": phoneme_start,
                        "end_ms": max(phoneme_start + 1, phoneme_end),
                        "source": timing_source,
                        "word_index": word_index,
                        "confidence": confidence,
                    }
                )
        return estimated

    def _valid_word_timestamp_items(
        self,
        raw_words: object,
        duration_ms: int | None,
    ) -> list[dict]:
        if not isinstance(raw_words, list):
            return []
        valid: list[dict] = []
        for item in raw_words:
            if not isinstance(item, dict):
                continue
            word = item.get("word")
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if not word or start_ms is None or end_ms is None:
                continue
            try:
                start = int(start_ms)
                end = int(end_ms)
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start:
                continue
            if duration_ms is not None and end > duration_ms + 180:
                continue
            valid.append({"word": str(word), "start_ms": start, "end_ms": end})
        return valid

    def _estimated_phoneme_labels(self, word: str) -> list[str]:
        letters = [character.lower() for character in word if character.isalpha()]
        if not letters:
            return []
        labels: list[str] = []
        index = 0
        vowels = set("aeiouy")
        while index < len(letters):
            current = letters[index]
            following = letters[index + 1] if index + 1 < len(letters) else ""
            pair = current + following
            if pair in {"ch", "sh", "th", "ph", "ng", "ck"}:
                labels.append(pair)
                index += 2
                continue
            if current in vowels:
                labels.append("vowel")
            else:
                labels.append(current)
            index += 1
        return labels

    def _viseme_for_phoneme(self, phoneme: str) -> str:
        label = phoneme.lower()
        if label in {"p", "b", "m"}:
            return "closed_lips"
        if label in {"f", "v", "ph"}:
            return "teeth_lip"
        if label in {"th", "θ", "ð"}:
            return "tongue_teeth"
        if label in {"s", "z", "sh", "ch", "j", "ʃ", "ʒ", "ç", "x"}:
            return "wide"
        if label in {"o", "u", "w", "ɔ", "ɔː", "oː", "uː", "ʊ", "ʊɐ"}:
            return "rounded"
        if label in {
            "vowel", "a", "e", "i", "y", "aː", "ɛ", "ɛː", "ə", "ɐ", "ɐː", "ɪ", "iː",
            "ʏ", "ø", "øː", "œ", "ɜ",
        }:
            return "open"
        if label in {"l", "n", "d", "t", "r", "ŋ", "ɐ̯"}:
            return "tongue_up"
        return "neutral"

    def run_audio_quality(
        self,
        episode: Episode,
        request: AudioQualityRequest,
        voicebox_endpoints: list[VoiceboxEndpoint],
        voice_profiles: list[VoiceProfile],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        qc = self._audio_media_qc(
            episode=episode,
            transcript=transcript,
            request=request,
            voicebox_endpoints=voicebox_endpoints,
            voice_profiles=voice_profiles,
        )
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="audio.qc.completed",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "status": qc.status,
                    "checked_count": qc.details["checked_audio_asset_count"],
                    "issue_count": qc.details["issue_count"],
                    "selection": self._selection_details(request),
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    async def sync_audio_results(
        self,
        episode: Episode,
        request: AudioResultSyncRequest,
        voicebox_endpoints: list[VoiceboxEndpoint],
        voice_profiles: list[VoiceProfile],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        target_assets = self._target_audio_assets(episode, transcript, request)
        if not target_assets:
            raise ValueError("target transcript has no audio assets to sync")

        sync_assets = [
            asset
            for asset in target_assets
            if asset.status in {"submitted", "running"}
            or request.include_completed
            or self._is_selective_sync_request(request)
        ]
        if not sync_assets:
            raise ValueError("target transcript has no submitted audio jobs to sync")

        endpoint_by_id = {endpoint.id: endpoint for endpoint in voicebox_endpoints}
        profile_by_id = {profile.id: profile for profile in voice_profiles}
        turn_by_id = {str(turn.id): turn for turn in transcript.turns}
        completed_count = 0
        failed_count = 0
        running_count = 0
        skipped_count = 0
        for asset in sync_assets:
            endpoint_id = asset.generation_metadata.get("voicebox_endpoint_id")
            endpoint = endpoint_by_id.get(str(endpoint_id)) if endpoint_id else None
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
                result = await self._poll_tts_result(endpoint, asset, str(job_id))
            except httpx.HTTPError as exc:
                asset.generation_metadata = {
                    **asset.generation_metadata,
                    "last_sync_error": str(exc),
                    "last_synced_at": datetime.now(UTC).isoformat(),
                }
                asset.updated_at = datetime.now(UTC)
                running_count += 1
                continue

            voice_profile_id = str(asset.generation_metadata.get("voice_profile_id") or "")
            result = await self._materialize_audio_result(
                endpoint,
                transcript,
                asset,
                result,
                voice_profile=profile_by_id.get(voice_profile_id),
            )
            result = self._with_timing_tracks(
                endpoint,
                turn_by_id.get(asset.source_entity_id),
                asset,
                result,
            )
            asset.status = result.status
            if result.storage_uri is not None:
                asset.storage_uri = result.storage_uri
            if result.mime_type is not None:
                asset.mime_type = result.mime_type
            if result.duration_ms is not None:
                asset.duration_ms = result.duration_ms
            if result.checksum is not None:
                asset.checksum = result.checksum
            sync_count = int(asset.generation_metadata.get("sync_attempt_count", 0))
            asset.generation_metadata = {
                **asset.generation_metadata,
                **result.metadata,
                "status": result.status,
                "sync_attempt_count": sync_count + 1,
                "last_synced_at": datetime.now(UTC).isoformat(),
                "completed_at": datetime.now(UTC).isoformat()
                if result.status == "completed"
                else asset.generation_metadata.get("completed_at"),
            }
            asset.updated_at = datetime.now(UTC)
            if result.status == "completed":
                completed_count += 1
            elif result.status in {"failed", "error"}:
                failed_count += 1
            else:
                running_count += 1

        timing_update = self._update_discussion_audio_timing(episode, transcript)
        generation_qc = self._audio_generation_qc(episode, transcript)
        episode.quality_results.append(generation_qc)
        media_qc = self._audio_media_qc(
            episode=episode,
            transcript=transcript,
            request=request,
            voicebox_endpoints=voicebox_endpoints,
            voice_profiles=voice_profiles,
        )
        episode.quality_results.append(media_qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="audio.jobs.synced",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                    "running_count": running_count,
                    "skipped_count": skipped_count,
                    "checked_count": media_qc.details["checked_audio_asset_count"],
                    "media_qc_status": media_qc.status,
                    "actual_duration_turns_updated": timing_update["turns_updated"],
                    "actual_duration_language": timing_update["language"],
                    "selection": self._selection_details(request),
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def _update_discussion_audio_timing(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> dict:
        session = episode.discussion_session
        if session is None:
            return {
                "schema_version": "discussion_audio_timing_update.v1",
                "language": transcript.language,
                "turns_updated": 0,
            }
        discussion_turn_by_id = {str(turn.id): turn for turn in session.turns}
        transcript_turn_by_id = {str(turn.id): turn for turn in transcript.turns}
        latest_completed_asset_by_turn_id = {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in transcript_turn_by_id
            and asset.status == "completed"
            and asset.duration_ms is not None
        }
        updated_count = 0
        for transcript_turn_id, asset in latest_completed_asset_by_turn_id.items():
            transcript_turn = transcript_turn_by_id[transcript_turn_id]
            duration_seconds = round(float(asset.duration_ms or 0) / 1000.0, 3)
            for source_turn_id in transcript_turn.source_discussion_turn_ids:
                discussion_turn = discussion_turn_by_id.get(str(source_turn_id))
                if discussion_turn is None:
                    continue
                durations_by_language = dict(
                    discussion_turn.generation_metadata.get(
                        "actual_audio_duration_seconds_by_language",
                        {},
                    )
                    or {}
                )
                previous = durations_by_language.get(transcript.language)
                durations_by_language[transcript.language] = duration_seconds
                discussion_turn.actual_audio_duration_seconds = duration_seconds
                discussion_turn.generation_metadata = {
                    **discussion_turn.generation_metadata,
                    "actual_audio_duration_seconds_by_language": durations_by_language,
                    "latest_actual_audio_duration_language": transcript.language,
                    "latest_audio_asset_id": str(asset.id),
                }
                if previous != duration_seconds:
                    updated_count += 1

        for balance in session.speaker_balance_state.values():
            balance.actual_speaking_seconds = 0
        for turn in session.turns:
            if turn.status == "excluded":
                continue
            durations_by_language = turn.generation_metadata.get(
                "actual_audio_duration_seconds_by_language",
                {},
            )
            duration_seconds = None
            if isinstance(durations_by_language, dict):
                duration_seconds = durations_by_language.get(transcript.language)
            if duration_seconds is None:
                duration_seconds = turn.actual_audio_duration_seconds
            if duration_seconds is None:
                continue
            balance = session.speaker_balance_state.get(turn.speaker_participant_id)
            if balance is None:
                continue
            balance.actual_speaking_seconds = round(
                balance.actual_speaking_seconds + float(duration_seconds),
                3,
            )
        session.controller_state = {
            **dict(session.controller_state or {}),
            "actual_audio_timing": {
                "schema_version": "discussion_audio_timing_update.v1",
                "language": transcript.language,
                "turns_updated": updated_count,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        }
        return session.controller_state["actual_audio_timing"]

    async def cancel_audio_jobs(
        self,
        episode: Episode,
        request: AudioCancellationRequest,
        voicebox_endpoints: list[VoiceboxEndpoint],
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        target_assets = self._target_audio_assets(episode, transcript, request)
        cancellable_assets = [
            asset
            for asset in target_assets
            if asset.status in {"submitted", "running", "failed"}
            or (request.reset_to_planned and asset.status == "cancelled")
        ]
        if not cancellable_assets:
            raise ValueError(
                "target transcript has no submitted, running, failed, or resettable "
                "cancelled audio jobs"
            )

        endpoint_by_id = {endpoint.id: endpoint for endpoint in voicebox_endpoints}
        cancelled_count = 0
        failed_count = 0
        remote_cancelled_count = 0
        remote_skipped_count = 0
        for asset in cancellable_assets:
            endpoint_id = asset.generation_metadata.get("voicebox_endpoint_id")
            endpoint = endpoint_by_id.get(str(endpoint_id)) if endpoint_id else None
            job_id = asset.generation_metadata.get("remote_job_id")
            previous_cancelled_job_id = asset.generation_metadata.get(
                "cancelled_remote_job_id"
            )
            cancel_response: dict | None = None
            remote_cancelled = False

            if endpoint is None or endpoint.adapter_type == "mock" or not endpoint.base_url:
                remote_skipped_count += 1
            elif not job_id:
                remote_skipped_count += 1
            else:
                try:
                    cancel_response = await self._cancel_tts_job(endpoint, asset, str(job_id))
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
                "cancelled_remote_job_id": job_id or previous_cancelled_job_id,
                "remote_cancelled": remote_cancelled,
                "remote_cancel_response": self._safe_provider_response_payload(
                    cancel_response or {}
                ),
                "remote_job_cancellation_required": False,
                "cancellation_attempt_count": cancel_count + 1,
                "cancelled_at": datetime.now(UTC).isoformat(),
                "reset_to_planned": request.reset_to_planned,
                "ready_for_retry": request.reset_to_planned,
            }
            asset.updated_at = datetime.now(UTC)
            cancelled_count += 1

        generation_qc = self._audio_generation_qc(episode, transcript)
        episode.quality_results.append(generation_qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="audio.jobs.cancelled",
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

    def _target_transcript(
        self,
        episode: Episode,
        request: (
            AudioAssetPlanRequest
            | AudioCancellationRequest
            | AudioGenerationRequest
            | AudioQualityRequest
            | AudioResultSyncRequest
        ),
    ) -> TranscriptVersion:
        if request.transcript_version_id is not None:
            return self._transcript_by_id(episode, request.transcript_version_id)
        if request.language is not None:
            matches = [
                transcript
                for transcript in episode.transcripts
                if transcript.language == request.language
                and transcript.type in {TranscriptType.localized, TranscriptType.broadcast}
                and transcript.status == "approved"
            ]
            if matches:
                return matches[-1]
            raise ValueError(f"no transcript found for language {request.language}")
        localized = [
            transcript
            for transcript in episode.transcripts
            if transcript.type == TranscriptType.localized and transcript.status == "approved"
        ]
        if localized:
            return localized[-1]
        canonical_id = episode.canonical_transcript_version_id
        if canonical_id is None:
            raise ValueError("episode has no canonical transcript")
        return self._transcript_by_id(episode, canonical_id)

    def _transcript_by_id(
        self,
        episode: Episode,
        transcript_id: UUID,
    ) -> TranscriptVersion:
        transcript = next(
            (item for item in episode.transcripts if item.id == transcript_id),
            None,
        )
        if transcript is None:
            raise ValueError("transcript not found")
        if transcript.status != "approved":
            raise ValueError("transcript must be approved before audio planning")
        return transcript

    def _existing_audio_asset(
        self,
        episode: Episode,
        language: str | None,
        source_entity_id: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.audio
                and asset.language == language
                and asset.source_entity_type == "transcript_turn"
                and asset.source_entity_id == source_entity_id
                and asset.status != "replaced"
            ),
            None,
        )

    def _estimate_duration_ms(self, text: str) -> int:
        seconds = len(text.split()) / self.settings.words_per_second
        return max(1, int(seconds * 1000))

    def _audio_plan_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> QualityResult:
        required_turn_ids = {
            str(turn.id) for turn in transcript.turns if turn.status != "excluded"
        }
        planned_turn_ids = {
            asset.source_entity_id
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in required_turn_ids
            and asset.status in {"planned", "submitted", "running", "completed"}
        }
        missing = sorted(required_turn_ids - planned_turn_ids)
        severity = QualitySeverity.pass_ if not missing else QualitySeverity.fail
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="audio_asset_plan_completeness",
            severity=severity,
            status=severity.value,
            score=1.0 if not missing else 0.0,
            details={
                "language": transcript.language,
                "required_audio_asset_count": len(required_turn_ids),
                "planned_audio_asset_count": len(planned_turn_ids),
                "missing_transcript_turn_ids": missing,
            },
        )

    def _target_audio_assets(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        request: (
            AudioCancellationRequest
            | AudioGenerationRequest
            | AudioQualityRequest
            | AudioResultSyncRequest
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
        turn_ids = {str(turn.id) for turn in transcript.turns if turn.status != "excluded"}
        participant_by_turn_id = {
            str(turn.id): turn.speaker_participant_id
            for turn in transcript.turns
            if turn.status != "excluded"
        }
        return [
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in turn_ids
            and asset.status in {
                "planned",
                "submitted",
                "running",
                "failed",
                "cancelled",
                "completed",
            }
            and (requested_asset_ids is None or str(asset.id) in requested_asset_ids)
            and (requested_turn_ids is None or asset.source_entity_id in requested_turn_ids)
            and (
                not requested_participant_ids
                or participant_by_turn_id.get(asset.source_entity_id) in requested_participant_ids
            )
            and (not request.failed_only or asset.status == "failed")
        ]

    async def _submit_tts(
        self,
        endpoint: VoiceboxEndpoint,
        voice_profile: VoiceProfile,
        transcript: TranscriptVersion,
        turn: TranscriptTurn,
        asset: Asset,
    ) -> TtsResult:
        if endpoint.adapter_type == "mock":
            return self._mock_tts(endpoint, voice_profile, transcript, turn, asset)
        if not endpoint.base_url:
            raise ValueError("voicebox endpoint requires base_url")
        if self._uses_audio_stream_submission(endpoint):
            return await self._submit_audio_stream_tts(
                endpoint,
                voice_profile,
                transcript,
                turn,
                asset,
            )

        headers = {"accept": "application/json", "content-type": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        payload = {
            "asset_id": str(asset.id),
            "transcript_turn_id": str(turn.id),
            "text": turn.text,
            "pronunciation_markup": turn.pronunciation_markup,
            "language": transcript.language,
            "voice": {
                "voice_profile_id": voice_profile.id,
                "voice_id": voice_profile.voice_id,
                "model_id": voice_profile.model_id,
                "rate": voice_profile.rate,
                "pitch": voice_profile.pitch,
                "prosody": voice_profile.prosody,
                "pronunciation_dictionary": voice_profile.pronunciation_dictionary,
            },
            "output": {"mime_type": asset.mime_type or "audio/wav"},
        }
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.post(f"{endpoint.base_url.rstrip('/')}/tts", json=payload)
            response.raise_for_status()
            data = response.json()
        audio_bytes = self._audio_bytes_from_payload(data)
        return TtsResult(
            status=self._normalize_tts_status(data.get("status", "completed")),
            storage_uri=self._result_storage_uri(data),
            mime_type=data.get("mime_type", asset.mime_type or "audio/wav"),
            duration_ms=data.get("duration_ms", self._estimate_duration_ms(turn.text)),
            checksum=data.get("checksum"),
            metadata={
                "adapter": "voicebox",
                "adapter_type": endpoint.adapter_type,
                "voicebox_endpoint_id": endpoint.id,
                "voice_profile_id": voice_profile.id,
                "remote_job_id": data.get("job_id"),
                "sample_rate": data.get("sample_rate"),
                "channels": data.get("channels"),
                "detected_language": data.get("detected_language"),
                "peak_dbfs": data.get("peak_dbfs"),
                "loudness_lufs": data.get("loudness_lufs"),
                "silence_ratio": data.get("silence_ratio"),
                "clipping_detected": data.get("clipping_detected", False),
                "word_timestamps": data.get("word_timestamps", []),
                "phoneme_timestamps": data.get("phoneme_timestamps", []),
                "provider_response": self._safe_provider_response_payload(data),
            },
            audio_bytes=audio_bytes,
        )

    async def _submit_audio_stream_tts(
        self,
        endpoint: VoiceboxEndpoint,
        voice_profile: VoiceProfile,
        transcript: TranscriptVersion,
        turn: TranscriptTurn,
        asset: Asset,
    ) -> TtsResult:
        headers = {
            "accept": self._capability_string(endpoint, "accept", "audio/wav"),
            "content-type": "application/json",
        }
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        payload = self._audio_stream_payload(endpoint, voice_profile, transcript, turn)
        path = self._capability_path(endpoint, "stream_generation_path", "/generate/stream")
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response, submission_attempts = await self._post_audio_stream_with_retry(
                client=client,
                endpoint=endpoint,
                url=f"{endpoint.base_url.rstrip('/')}{path}",
                payload=payload,
            )
            response.raise_for_status()
            timing_metadata = await self._fetch_b1_stream_timing(
                client=client,
                endpoint=endpoint,
                response=response,
            )
        mime_type = response.headers.get("content-type", asset.mime_type or "audio/wav").split(";")[
            0
        ]
        if not response.content:
            raise ValueError("audio stream provider returned empty audio content")
        if not mime_type.startswith("audio/"):
            raise ValueError(f"audio stream provider returned non-audio content type {mime_type}")
        return TtsResult(
            status="completed",
            storage_uri=None,
            mime_type=mime_type or asset.mime_type or "audio/wav",
            duration_ms=self._estimate_duration_ms(turn.text),
            checksum=None,
            metadata={
                "adapter": "voicebox",
                "adapter_type": endpoint.adapter_type,
                "voicebox_endpoint_id": endpoint.id,
                "voice_profile_id": voice_profile.id,
                "remote_profile_id": voice_profile.voice_id,
                "engine": payload.get("engine"),
                "normalize": payload.get("normalize"),
                "effects_chain_configured": bool(payload.get("effects_chain")),
                "stream_generation_path": path,
                "stream_generation_attempts": submission_attempts,
                "sample_rate": None,
                "channels": None,
                "detected_language": payload.get("language"),
                "peak_dbfs": None,
                "loudness_lufs": None,
                "silence_ratio": None,
                "clipping_detected": False,
                "word_timestamps": [],
                "phoneme_timestamps": [],
                **timing_metadata,
                "provider_response": self._safe_provider_response_payload(
                    {
                        "status": "completed",
                        "content_type": mime_type,
                        "status_code": response.status_code,
                        "content_length": len(response.content),
                        "generation_id": response.headers.get("x-b1-generation-id"),
                        "audio_sha256": response.headers.get("x-b1-audio-sha256"),
                        "timing_url": response.headers.get("x-b1-timing-url"),
                    }
                ),
            },
            audio_bytes=response.content,
        )

    async def _post_audio_stream_with_retry(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: VoiceboxEndpoint,
        url: str,
        payload: dict,
    ) -> tuple[httpx.Response, int]:
        """Retry only B1's transient stream-admission responses."""
        default_attempts = 8 if endpoint.adapter_type == "b1_voice_stream" else 4
        attempts = max(
            1,
            int(endpoint.capabilities.get("stream_generation_retry_attempts", default_attempts)),
        )
        base_delay_seconds = max(
            0.0,
            float(endpoint.capabilities.get("stream_generation_retry_backoff_seconds", 5)),
        )
        max_backoff_seconds = max(
            0.0,
            float(endpoint.capabilities.get("stream_generation_retry_max_backoff_seconds", 30)),
        )
        retry_statuses = {409, 429, 502, 503, 504}
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            response = await client.post(url, json=payload)
            if response.status_code not in retry_statuses or attempt == attempts:
                return response, attempt
            retry_after_seconds = self._retry_after_seconds(response)
            delay_seconds = (
                retry_after_seconds
                if retry_after_seconds is not None
                else min(
                    base_delay_seconds * (2 ** (attempt - 1)),
                    max_backoff_seconds,
                )
            )
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        assert response is not None
        return response, attempts

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        raw_value = response.headers.get("retry-after")
        if not raw_value:
            return None
        try:
            return max(0.0, float(raw_value))
        except ValueError:
            return None

    @staticmethod
    def _is_b1_scheduler_busy_response(response: httpx.Response) -> bool:
        """Recognize B1's transient GPU-lease response without persisting its body."""
        if response.status_code != 409:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        detail = payload.get("detail")
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("reason") or ""
        else:
            message = detail or ""
        normalized = str(message).strip().casefold()
        return "gpu scheduler lease is held by another owner" in normalized

    async def _fetch_b1_stream_timing(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: VoiceboxEndpoint,
        response: httpx.Response,
    ) -> dict:
        generation_id = str(response.headers.get("x-b1-generation-id") or "").strip()
        timing_url_raw = str(response.headers.get("x-b1-timing-url") or "").strip()
        advertised_checksum = self._normalized_sha256(response.headers.get("x-b1-audio-sha256"))
        actual_checksum = hashlib.sha256(response.content).hexdigest()
        if not generation_id or not timing_url_raw:
            return {}
        if advertised_checksum and advertised_checksum != actual_checksum:
            return {
                "b1_timing": {
                    "status": "invalid",
                    "reason": "stream_audio_checksum_mismatch",
                    "generation_id": generation_id,
                }
            }

        timing_url = urljoin(f"{endpoint.base_url.rstrip('/')}/", timing_url_raw)
        parsed_base = urlparse(endpoint.base_url)
        parsed_timing = urlparse(timing_url)
        if (
            parsed_timing.scheme not in {"http", "https"}
            or parsed_timing.netloc != parsed_base.netloc
        ):
            return {
                "b1_timing": {
                    "status": "invalid",
                    "reason": "timing_url_not_same_origin",
                    "generation_id": generation_id,
                }
            }

        attempts = max(1, int(endpoint.capabilities.get("timing_poll_attempts", 3)))
        poll_delay_seconds = max(
            0.0,
            float(endpoint.capabilities.get("timing_poll_interval_seconds", 0.5)),
        )
        timing_response: httpx.Response | None = None
        for attempt in range(attempts):
            timing_response = await client.get(
                timing_url,
                headers={"accept": "application/json"},
            )
            if timing_response.status_code == 200:
                break
            if timing_response.status_code not in {202, 404, 409, 425}:
                return {
                    "b1_timing": {
                        "status": "unavailable",
                        "reason": f"timing_http_{timing_response.status_code}",
                        "generation_id": generation_id,
                    }
                }
            if attempt < attempts - 1 and poll_delay_seconds:
                await asyncio.sleep(poll_delay_seconds)
        if timing_response is None or timing_response.status_code != 200:
            return {
                "b1_timing": {
                    "status": "pending",
                    "generation_id": generation_id,
                    "poll_attempts": attempts,
                }
            }
        try:
            payload = timing_response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return {
                "b1_timing": {
                    "status": "invalid",
                    "reason": "timing_response_not_json_object",
                    "generation_id": generation_id,
                }
            }
        timing_generation_id = str(payload.get("generation_id") or "").strip()
        timing_checksum = self._normalized_sha256(payload.get("audio_sha256"))
        if timing_generation_id != generation_id or (
            timing_checksum is not None and timing_checksum != actual_checksum
        ):
            return {
                "b1_timing": {
                    "status": "invalid",
                    "reason": "timing_binding_mismatch",
                    "generation_id": generation_id,
                }
            }

        words = payload.get("word_timestamps")
        phonemes = payload.get("phoneme_timestamps")
        characters = payload.get("character_timestamps")
        if not isinstance(words, list) or not isinstance(phonemes, list):
            return {
                "b1_timing": {
                    "status": "invalid",
                    "reason": "timing_tracks_missing",
                    "generation_id": generation_id,
                }
            }
        word_confidence = self._timestamp_confidence(words, default=0.0)
        phoneme_confidence = self._timestamp_confidence(phonemes, default=0.5)
        return {
            "word_timestamps": words,
            "phoneme_timestamps": [
                {**item, "source": "b1_ipa_from_ctc_word_windows"}
                for item in phonemes
                if isinstance(item, dict)
            ],
            "character_timestamps": characters if isinstance(characters, list) else [],
            "word_timing": {
                "source": "b1_ctc_forced_alignment",
                "confidence": word_confidence,
                "word_count": len(words),
                "duration_ms": payload.get("duration_ms"),
            },
            "phoneme_timing_source": "b1_ipa_from_ctc_word_windows",
            "phoneme_timing_confidence": phoneme_confidence,
            "b1_timing": {
                "status": "completed",
                "generation_id": generation_id,
                "audio_sha256": actual_checksum,
                "timing_url": timing_url_raw,
                "schema_version": payload.get("schema_version"),
                "timing_method": payload.get("timing_method"),
                "timing_precision": payload.get("timing_precision"),
                "phoneme_alphabet": payload.get("phoneme_alphabet"),
                "alignment_model": payload.get("alignment_model"),
                "alignment_device": payload.get("alignment_device"),
                "checksum_bound": True,
            },
        }

    def _normalized_sha256(self, value: object) -> str | None:
        candidate = str(value or "").strip().lower()
        if candidate.startswith("sha256:"):
            candidate = candidate.removeprefix("sha256:")
        if len(candidate) != 64 or any(
            character not in "0123456789abcdef" for character in candidate
        ):
            return None
        return candidate

    def _audio_stream_payload(
        self,
        endpoint: VoiceboxEndpoint,
        voice_profile: VoiceProfile,
        transcript: TranscriptVersion,
        turn: TranscriptTurn,
    ) -> dict:
        prosody = voice_profile.prosody if isinstance(voice_profile.prosody, dict) else {}
        engine = (
            prosody.get("engine")
            or voice_profile.model_id
            or endpoint.capabilities.get("default_engine")
            or "chatterbox"
        )
        normalize = prosody.get("normalize")
        if not isinstance(normalize, bool):
            normalize = endpoint.capabilities.get("normalize_default")
        effects_chain = prosody.get("effects_chain")
        if not isinstance(normalize, bool):
            normalize = False
        if not isinstance(effects_chain, list):
            default_effects = endpoint.capabilities.get("effects_chain_default")
            effects_chain = default_effects if isinstance(default_effects, list) else []
        payload = {
            "profile_id": voice_profile.voice_id,
            "text": turn.text,
            "language": voice_profile.language or transcript.language,
            "engine": str(engine),
            "normalize": normalize,
            "effects_chain": effects_chain,
        }
        request_extra = prosody.get("request_extra")
        if isinstance(request_extra, dict):
            payload.update(request_extra)
        return payload

    async def _poll_tts_result(
        self,
        endpoint: VoiceboxEndpoint,
        asset: Asset,
        job_id: str,
    ) -> TtsResult:
        if not endpoint.base_url:
            raise ValueError("voicebox endpoint requires base_url")

        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        path_template = endpoint.capabilities.get("job_status_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = "/tts/jobs/{job_id}"
        status_path = path_template.format(job_id=job_id, asset_id=asset.id)
        timeout = httpx.Timeout(endpoint.default_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            headers=headers,
            verify=self._endpoint_verify(endpoint),
        ) as client:
            response = await client.get(f"{endpoint.base_url.rstrip('/')}{status_path}")
            response.raise_for_status()
            data = response.json()
        return self._tts_result_from_payload(
            endpoint=endpoint,
            asset=asset,
            payload=data,
            default_duration_ms=asset.duration_ms,
            default_job_id=job_id,
        )

    async def _cancel_existing_job_before_retry(
        self,
        endpoint: VoiceboxEndpoint,
        asset: Asset,
    ) -> bool:
        job_id = asset.generation_metadata.get("remote_job_id")
        if endpoint.adapter_type == "mock" or not endpoint.base_url or not job_id:
            return True
        previous_status = asset.status
        try:
            cancel_response = await self._cancel_tts_job(endpoint, asset, str(job_id))
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
            "previous_status": previous_status,
            "remote_job_id": None,
            "cancelled_remote_job_id": job_id,
            "remote_cancelled": True,
                "remote_cancel_response": self._safe_provider_response_payload(
                    cancel_response or {}
                ),
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

    async def _cancel_tts_job(
        self,
        endpoint: VoiceboxEndpoint,
        asset: Asset,
        job_id: str,
    ) -> dict:
        if not endpoint.base_url:
            raise ValueError("voicebox endpoint requires base_url")

        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        path_template = endpoint.capabilities.get("job_cancel_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = endpoint.capabilities.get("cancellation_path_template")
        if not isinstance(path_template, str) or not path_template:
            path_template = "/tts/jobs/{job_id}"
        cancel_path = path_template.format(job_id=job_id, asset_id=asset.id)
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
                    f"{endpoint.base_url.rstrip('/')}{cancel_path}",
                    json=payload,
                )
            else:
                response = await client.request(
                    normalized_method,
                    f"{endpoint.base_url.rstrip('/')}{cancel_path}",
                )
            response.raise_for_status()
            if not response.content:
                return {"status": "cancelled"}
            try:
                data = response.json()
            except ValueError:
                return {"status": "cancelled"}
            return data if isinstance(data, dict) else {"status": "cancelled"}

    def _tts_result_from_payload(
        self,
        endpoint: VoiceboxEndpoint,
        asset: Asset,
        payload: dict,
        default_duration_ms: int | None,
        default_job_id: str | None = None,
    ) -> TtsResult:
        status = self._normalize_tts_status(payload.get("status", "completed"))
        audio_bytes = self._audio_bytes_from_payload(payload)
        return TtsResult(
            status=status,
            storage_uri=self._result_storage_uri(payload),
            mime_type=payload.get("mime_type", asset.mime_type or "audio/wav"),
            duration_ms=payload.get("duration_ms", default_duration_ms),
            checksum=payload.get("checksum"),
            metadata={
                "adapter": "voicebox",
                "adapter_type": endpoint.adapter_type,
                "voicebox_endpoint_id": endpoint.id,
                "remote_job_id": payload.get("job_id", default_job_id),
                "sample_rate": payload.get("sample_rate"),
                "channels": payload.get("channels"),
                "detected_language": payload.get("detected_language"),
                "peak_dbfs": payload.get("peak_dbfs"),
                "loudness_lufs": payload.get("loudness_lufs"),
                "silence_ratio": payload.get("silence_ratio"),
                "clipping_detected": payload.get("clipping_detected", False),
                "word_timestamps": payload.get("word_timestamps", []),
                "phoneme_timestamps": payload.get("phoneme_timestamps", []),
                "provider_response": self._safe_provider_response_payload(payload),
            },
            audio_bytes=audio_bytes,
        )

    def _safe_provider_response_payload(self, payload: object) -> object:
        return safe_provider_response_payload(payload)

    def _is_sensitive_provider_response_key(self, key: str) -> bool:
        return is_sensitive_provider_response_key(key)

    def _result_storage_uri(self, payload: dict) -> str | None:
        for key in ("storage_uri", "audio_url", "result_url", "media_url", "download_url"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _audio_bytes_from_payload(self, payload: dict) -> bytes | None:
        audio_base64 = payload.get("audio_base64")
        if not isinstance(audio_base64, str) or not audio_base64:
            return None
        try:
            return base64.b64decode(audio_base64, validate=True)
        except (binascii.Error, ValueError):
            return None

    def _normalize_tts_status(self, status: str | None) -> str:
        normalized = (status or "completed").lower()
        if normalized in {"succeeded", "success", "done"}:
            return "completed"
        if normalized in {"queued", "pending", "processing"}:
            return "submitted"
        if normalized in {"errored", "error"}:
            return "failed"
        if normalized in {"canceled"}:
            return "cancelled"
        return normalized

    def _uses_audio_stream_submission(self, endpoint: VoiceboxEndpoint) -> bool:
        return endpoint.adapter_type == "b1_voice_stream" or (
            endpoint.capabilities.get("response_mode") == "audio_stream"
        )

    def _ensure_stream_endpoint_connectivity(
        self,
        *,
        episode: Episode,
        transcript: TranscriptVersion,
        assets: list[Asset],
        endpoint_by_id: dict[str, VoiceboxEndpoint],
        profile_by_id: dict[str, VoiceProfile],
    ) -> None:
        checked_endpoint_ids: set[str] = set()
        turn_by_id = {str(turn.id): turn for turn in transcript.turns}
        for asset in assets:
            turn = turn_by_id.get(asset.source_entity_id)
            if turn is None:
                continue
            participant = self._participant_by_id(episode, turn.speaker_participant_id)
            voice_profile_id = self._voice_profile_id_for_participant(participant, profile_by_id)
            if voice_profile_id is None:
                continue
            voice_profile = profile_by_id.get(voice_profile_id)
            if voice_profile is None:
                continue
            endpoint = endpoint_by_id.get(voice_profile.voicebox_endpoint_id)
            if endpoint is None or endpoint.id in checked_endpoint_ids:
                continue
            checked_endpoint_ids.add(endpoint.id)
            if not self._uses_audio_stream_submission(endpoint) or not bool(
                endpoint.capabilities.get("require_base_url_dns_resolution")
            ):
                continue
            dns_result = self._base_url_dns_resolution(endpoint)
            if not dns_result or dns_result.get("resolved"):
                continue
            host = dns_result.get("host") or "unknown"
            error = dns_result.get("error") or "unresolved"
            raise ValueError(
                "voicebox endpoint base URL host could not be resolved; "
                f"endpoint_id={endpoint.id}; host={host}; error={error}"
            )

    def _base_url_dns_resolution(self, endpoint: VoiceboxEndpoint) -> dict[str, object]:
        if not endpoint.base_url:
            return {"resolved": False, "error": "missing_base_url"}
        parsed = urlparse(endpoint.base_url)
        host = parsed.hostname
        if not host:
            return {"resolved": False, "error": "missing_host"}
        try:
            addresses = sorted(
                {
                    item[4][0]
                    for item in socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
                    if item[4]
                }
            )
        except OSError as exc:
            return {"resolved": False, "host": host, "error": str(exc)}
        return {
            "resolved": bool(addresses),
            "host": host,
            "address_count": len(addresses),
            "addresses": addresses[:5],
        }

    def _endpoint_verify(self, endpoint: VoiceboxEndpoint) -> bool | str:
        ca_cert_path = endpoint.capabilities.get("tls_ca_cert_path")
        if isinstance(ca_cert_path, str) and ca_cert_path.strip():
            return ca_cert_path.strip()
        return True

    def _transcription_base_url(self, endpoint: VoiceboxEndpoint) -> str | None:
        configured = self._capability_string(endpoint, "transcription_base_url", "")
        if configured:
            return configured.rstrip("/")
        if endpoint.adapter_type == "b1_voice_stream" and endpoint.base_url:
            parsed = urlparse(endpoint.base_url)
            if parsed.hostname == "voice.ai.b1.germering":
                return "https://api.ai.b1.germering"
        return None

    def _capability_path(
        self,
        endpoint: VoiceboxEndpoint,
        key: str,
        default: str,
    ) -> str:
        raw_path = endpoint.capabilities.get(key, default)
        path = str(raw_path or default)
        return path if path.startswith("/") else f"/{path}"

    def _capability_string(
        self,
        endpoint: VoiceboxEndpoint,
        key: str,
        default: str,
    ) -> str:
        value = endpoint.capabilities.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    @staticmethod
    def _spoken_text_tokens(text: str) -> list[str]:
        return re.findall(r"[\w]+(?:['-][\w]+)?", text.casefold(), flags=re.UNICODE)

    @classmethod
    def _spoken_text_qc(cls, expected_text: str, spoken_text: str) -> dict:
        expected_tokens = cls._spoken_text_tokens(expected_text)
        spoken_tokens = cls._spoken_text_tokens(spoken_text)
        if not expected_tokens or not spoken_tokens:
            return {
                "passed": False,
                "reason_codes": ["empty_expected_or_transcribed_text"],
                "expected_word_count": len(expected_tokens),
                "transcribed_word_count": len(spoken_tokens),
            }

        matcher = SequenceMatcher(a=expected_tokens, b=spoken_tokens, autojunk=False)
        matching_tokens = sum(block.size for block in matcher.get_matching_blocks())
        expected_coverage = matching_tokens / len(expected_tokens)
        transcript_precision = matching_tokens / len(spoken_tokens)
        repeated_phrases = cls._unexpected_repeated_phrases(expected_tokens, spoken_tokens)
        reason_codes: list[str] = []
        if expected_coverage < 0.84:
            reason_codes.append("expected_script_coverage_too_low")
        if transcript_precision < 0.84:
            reason_codes.append("unexpected_spoken_content")
        if repeated_phrases:
            reason_codes.append("repeated_spoken_phrase")
        return {
            "passed": not reason_codes,
            "reason_codes": reason_codes,
            "expected_word_count": len(expected_tokens),
            "transcribed_word_count": len(spoken_tokens),
            "matching_word_count": matching_tokens,
            "expected_script_coverage": round(expected_coverage, 4),
            "transcript_precision": round(transcript_precision, 4),
            "repeated_phrases": repeated_phrases,
        }

    @staticmethod
    def _unexpected_repeated_phrases(
        expected_tokens: list[str], spoken_tokens: list[str]
    ) -> list[str]:
        for width in range(min(12, len(spoken_tokens) // 2), 3, -1):
            positions: dict[tuple[str, ...], int] = {}
            for index in range(len(spoken_tokens) - width + 1):
                phrase = tuple(spoken_tokens[index : index + width])
                previous = positions.get(phrase)
                expected_occurrences = sum(
                    tuple(expected_tokens[candidate : candidate + width]) == phrase
                    for candidate in range(len(expected_tokens) - width + 1)
                )
                if (
                    previous is not None
                    and index - previous >= width
                    and expected_occurrences < 2
                ):
                    return [" ".join(phrase)]
                positions.setdefault(phrase, index)
        return []

    def _mock_tts(
        self,
        endpoint: VoiceboxEndpoint,
        voice_profile: VoiceProfile,
        transcript: TranscriptVersion,
        turn: TranscriptTurn,
        asset: Asset,
    ) -> TtsResult:
        seed = "|".join(
            [
                str(asset.id),
                transcript.language,
                voice_profile.voice_id,
                turn.text,
            ]
        )
        checksum = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        duration_ms = self._estimate_duration_ms(turn.text)
        sample_rate = 48000
        channels = 1
        audio_bytes = self._mock_wav_bytes(
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
        )
        return TtsResult(
            status="completed",
            storage_uri=None,
            mime_type="audio/wav",
            duration_ms=duration_ms,
            checksum=checksum,
            metadata={
                "adapter": "voicebox",
                "adapter_type": endpoint.adapter_type,
                "voicebox_endpoint_id": endpoint.id,
                "voice_profile_id": voice_profile.id,
                "remote_job_id": f"mock-tts-{asset.id}",
                "sample_rate": sample_rate,
                "channels": channels,
                "detected_language": transcript.language,
                "peak_dbfs": -3.0,
                "loudness_lufs": -18.0,
                "silence_ratio": 0.05,
                "clipping_detected": False,
                "word_timestamps": self._mock_word_timestamps(turn.text, duration_ms),
                "phoneme_timestamps": [],
            },
            audio_bytes=audio_bytes,
        )

    def _mock_wav_bytes(
        self,
        duration_ms: int,
        sample_rate: int,
        channels: int,
    ) -> bytes:
        frame_count = max(1, int(sample_rate * (duration_ms / 1000)))
        buffer = io.BytesIO()
        amplitude = 0.16
        frequency = 220
        pattern_frames = min(sample_rate, frame_count)
        pattern = array(
            "h",
            (
                int(
                    32767
                    * amplitude
                    * math.sin(2 * math.pi * frequency * index / sample_rate)
                )
                for index in range(pattern_frames)
            ),
        )
        pattern_bytes = pattern.tobytes() * channels
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            full_patterns, remainder = divmod(frame_count, pattern_frames)
            if full_patterns:
                audio.writeframesraw(pattern_bytes * full_patterns)
            if remainder:
                audio.writeframesraw(pattern[:remainder].tobytes() * channels)
        return buffer.getvalue()

    def _mock_word_timestamps(self, text: str, duration_ms: int) -> list[dict]:
        words = text.split()
        if not words:
            return []
        step = max(1, duration_ms // len(words))
        return [
            {
                "word": word,
                "start_ms": index * step,
                "end_ms": min(duration_ms, (index + 1) * step),
            }
            for index, word in enumerate(words)
        ]

    def _audio_generation_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> QualityResult:
        required_turn_ids = {
            str(turn.id) for turn in transcript.turns if turn.status != "excluded"
        }
        completed_assets = [
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in required_turn_ids
            and asset.status == "completed"
        ]
        completed_turn_ids = {asset.source_entity_id for asset in completed_assets}
        missing = sorted(required_turn_ids - completed_turn_ids)
        invalid_assets = [
            {
                "asset_id": str(asset.id),
                "source_entity_id": asset.source_entity_id,
                "issue": "missing_audio_storage_or_duration",
            }
            for asset in completed_assets
            if not asset.storage_uri or not asset.duration_ms or not asset.checksum
        ]
        if missing or invalid_assets:
            severity = QualitySeverity.fail
        else:
            severity = QualitySeverity.pass_
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="audio_generation_completeness",
            severity=severity,
            status=severity.value,
            score=1.0 if severity == QualitySeverity.pass_ else 0.0,
            details={
                "language": transcript.language,
                "required_audio_asset_count": len(required_turn_ids),
                "completed_audio_asset_count": len(completed_turn_ids),
                "missing_transcript_turn_ids": missing,
                "invalid_assets": invalid_assets,
            },
        )

    def _audio_media_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        request: AudioGenerationRequest | AudioQualityRequest | AudioResultSyncRequest,
        voicebox_endpoints: list[VoiceboxEndpoint],
        voice_profiles: list[VoiceProfile],
    ) -> QualityResult:
        target_assets = self._target_audio_assets(episode, transcript, request)
        if not target_assets:
            raise ValueError("target transcript has no audio assets to check")

        endpoint_by_id = {endpoint.id: endpoint for endpoint in voicebox_endpoints}
        profile_by_id = {profile.id: profile for profile in voice_profiles}
        turn_by_id = {str(turn.id): turn for turn in transcript.turns}
        issues: list[dict] = []
        checked_count = 0
        completed_count = 0
        probed_count = 0
        waveform_analyzed_count = 0
        loudness_analyzed_count = 0
        loudness_normalization_recommended_count = 0
        downloaded_remote_result_count = 0
        phoneme_timed_asset_count = 0
        provider_phoneme_timed_asset_count = 0
        estimated_phoneme_timed_asset_count = 0
        phoneme_timing_missing_count = 0

        for asset in target_assets:
            checked_count += 1
            turn = turn_by_id.get(asset.source_entity_id)
            if turn is None:
                issues.append(self._audio_issue(asset, "fail", "missing_source_turn"))
                continue
            if asset.status != "completed":
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "audio_not_completed",
                        {"status": asset.status},
                    )
                )
                continue

            completed_count += 1
            metadata = asset.generation_metadata
            if metadata.get("remote_result_downloaded"):
                downloaded_remote_result_count += 1
            phoneme_timing = metadata.get("phoneme_timing")
            if isinstance(phoneme_timing, dict) and phoneme_timing.get("phoneme_count", 0):
                phoneme_timed_asset_count += 1
                if phoneme_timing.get("source") == "provider_phoneme_timestamps":
                    provider_phoneme_timed_asset_count += 1
                elif phoneme_timing.get("source") in {
                    "estimated_from_word_timestamps",
                    "estimated_from_transcript_text",
                }:
                    estimated_phoneme_timed_asset_count += 1
            else:
                phoneme_timing_missing_count += 1
            probe = (
                self.audio_probe.probe_uri(asset.storage_uri, asset.mime_type)
                if asset.storage_uri and self.object_store.path_for_uri(asset.storage_uri)
                else None
            )
            if probe is not None:
                probed_count += 1
                if probe.silence_ratio is not None or probe.peak_dbfs is not None:
                    waveform_analyzed_count += 1
                if probe.loudness_source == "ffmpeg_loudnorm":
                    loudness_analyzed_count += 1
                    if (
                        probe.loudness_normalization_gain_db is not None
                        and abs(float(probe.loudness_normalization_gain_db)) > 1.0
                    ):
                        loudness_normalization_recommended_count += 1
                for warning in probe.probe_warnings:
                    issues.append(
                        self._audio_issue(
                            asset,
                            "warning",
                            "media_probe_warning",
                            {"warning": warning, "probe_tool": probe.probe_tool},
                        )
                    )
                if probe.duration_ms is None:
                    issues.append(
                        self._audio_issue(
                            asset,
                            "fail",
                            "media_probe_missing_duration",
                            {"probe_tool": probe.probe_tool},
                        )
                    )
            endpoint = endpoint_by_id.get(str(metadata.get("voicebox_endpoint_id")))
            expected_profile_id = self._expected_voice_profile_id(
                episode=episode,
                turn=turn,
                profile_by_id=profile_by_id,
            )
            expected_duration_ms = self._estimate_duration_ms(turn.text)
            actual_duration_ms = (
                probe.duration_ms if probe and probe.duration_ms else asset.duration_ms
            )
            actual_mime_type = probe.mime_type if probe and probe.mime_type else asset.mime_type
            sample_rate = (
                probe.sample_rate
                if probe is not None and probe.sample_rate is not None
                else metadata.get("sample_rate")
            )
            channels = (
                probe.channels
                if probe is not None and probe.channels is not None
                else metadata.get("channels")
            )
            clipping_detected = (
                probe.clipping_detected
                if probe is not None and probe.clipping_detected is not None
                else metadata.get("clipping_detected")
            )
            peak_dbfs = (
                probe.peak_dbfs
                if probe is not None and probe.peak_dbfs is not None
                else metadata.get("peak_dbfs")
            )
            silence_ratio = (
                probe.silence_ratio
                if probe is not None and probe.silence_ratio is not None
                else metadata.get("silence_ratio")
            )
            loudness_lufs = (
                probe.loudness_lufs
                if probe is not None and probe.loudness_lufs is not None
                else metadata.get("loudness_lufs")
            )
            loudness_source = (
                probe.loudness_source
                if probe is not None and probe.loudness_source is not None
                else metadata.get("loudness_source")
            )
            true_peak_dbtp = (
                probe.true_peak_dbtp
                if probe is not None and probe.true_peak_dbtp is not None
                else metadata.get("true_peak_dbtp")
            )
            loudness_normalization_gain_db = (
                probe.loudness_normalization_gain_db
                if probe is not None and probe.loudness_normalization_gain_db is not None
                else metadata.get("loudness_normalization_gain_db")
            )
            loudness_target_lufs = (
                probe.loudness_target_lufs
                if probe is not None and probe.loudness_target_lufs is not None
                else metadata.get("loudness_target_lufs")
            )
            true_peak_target_dbtp = (
                probe.true_peak_target_dbtp
                if probe is not None and probe.true_peak_target_dbtp is not None
                else metadata.get("true_peak_target_dbtp")
            )

            if not asset.storage_uri:
                issues.append(self._audio_issue(asset, "fail", "missing_storage_uri"))
            if not asset.checksum:
                issues.append(self._audio_issue(asset, "fail", "missing_checksum"))
            if not actual_duration_ms or actual_duration_ms <= 0:
                issues.append(self._audio_issue(asset, "fail", "missing_duration"))
            elif not self._duration_is_plausible(actual_duration_ms, expected_duration_ms):
                duration_severity = (
                    "warning"
                    if probe is not None
                    and probe.duration_ms is not None
                    and actual_duration_ms > 0
                    and actual_mime_type
                    and actual_mime_type.startswith("audio/")
                    else "fail"
                )
                issues.append(
                    self._audio_issue(
                        asset,
                        duration_severity,
                        "unexpected_duration",
                        {
                            "duration_ms": actual_duration_ms,
                            "expected_duration_ms": expected_duration_ms,
                            "duration_source": "media_probe" if probe else "asset_metadata",
                        },
                    )
                )

            if not actual_mime_type or not actual_mime_type.startswith("audio/"):
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "wrong_format",
                        {"mime_type": actual_mime_type},
                    )
            )
            elif endpoint is not None:
                supported_formats = endpoint.capabilities.get("formats")
                if (
                    isinstance(supported_formats, list)
                    and actual_mime_type not in supported_formats
                ):
                    issues.append(
                        self._audio_issue(
                            asset,
                            "fail",
                            "unsupported_format",
                            {
                                "mime_type": actual_mime_type,
                                "supported_formats": supported_formats,
                            },
                        )
                    )

            if sample_rate is None:
                issues.append(self._audio_issue(asset, "warning", "missing_sample_rate"))
            else:
                expected_sample_rates = self._expected_sample_rates(endpoint)
                if int(sample_rate) not in expected_sample_rates:
                    issues.append(
                        self._audio_issue(
                            asset,
                            "fail",
                            "sample_rate_mismatch",
                            {
                                "sample_rate": sample_rate,
                                "expected_sample_rates": expected_sample_rates,
                            },
                        )
                    )

            if channels is None:
                issues.append(self._audio_issue(asset, "warning", "missing_channel_count"))
            elif int(channels) <= 0:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "invalid_channel_count",
                        {"channels": channels},
                    )
                )

            detected_language = metadata.get("detected_language")
            if detected_language and detected_language != transcript.language:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "wrong_language",
                        {
                            "detected_language": detected_language,
                            "expected_language": transcript.language,
                        },
                    )
                )

            actual_profile_id = metadata.get("voice_profile_id")
            if expected_profile_id and actual_profile_id != expected_profile_id:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "inconsistent_voice_profile",
                        {
                            "voice_profile_id": actual_profile_id,
                            "expected_voice_profile_id": expected_profile_id,
                        },
                    )
                )

            if clipping_detected:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "clipping_detected",
                        {"source": "media_probe" if probe else "provider_metadata"},
                    )
                )
            if peak_dbfs is not None and float(peak_dbfs) >= -0.1:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "clipping_risk",
                        {
                            "peak_dbfs": peak_dbfs,
                            "source": "media_probe" if probe else "provider_metadata",
                        },
                    )
                )
            if silence_ratio is not None and float(silence_ratio) > 0.35:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "excessive_silence",
                        {
                            "silence_ratio": silence_ratio,
                            "source": "media_probe" if probe else "provider_metadata",
                        },
                    )
                )
            if loudness_lufs is not None and not -28 <= float(loudness_lufs) <= -12:
                issues.append(
                    self._audio_issue(
                        asset,
                        "warning",
                        "loudness_deviation",
                        {
                            "loudness_lufs": loudness_lufs,
                            "loudness_source": loudness_source,
                            "source": "media_probe" if probe else "provider_metadata",
                            "target_lufs": loudness_target_lufs,
                            "normalization_gain_db": loudness_normalization_gain_db,
                        },
                    )
                )
            if (
                true_peak_dbtp is not None
                and true_peak_target_dbtp is not None
                and float(true_peak_dbtp) > float(true_peak_target_dbtp)
            ):
                issues.append(
                    self._audio_issue(
                        asset,
                        "warning",
                        "true_peak_limit_exceeded",
                        {
                            "true_peak_dbtp": true_peak_dbtp,
                            "true_peak_target_dbtp": true_peak_target_dbtp,
                            "source": "media_probe" if probe else "provider_metadata",
                        },
                    )
                )
            issues.extend(self._timestamp_issues(asset))
            issues.extend(self._phoneme_timing_issues(asset))

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
            check_type="audio_media_integrity",
            severity=severity,
            status=severity.value,
            score=1.0 if severity == QualitySeverity.pass_ else 0.5
            if severity == QualitySeverity.warning
            else 0.0,
            details={
                "language": transcript.language,
                "checked_audio_asset_count": checked_count,
                "completed_audio_asset_count": completed_count,
                "probed_audio_asset_count": probed_count,
                "waveform_analyzed_audio_asset_count": waveform_analyzed_count,
                "loudness_analyzed_audio_asset_count": loudness_analyzed_count,
                "loudness_normalization_recommended_audio_asset_count": (
                    loudness_normalization_recommended_count
                ),
                "downloaded_remote_result_count": downloaded_remote_result_count,
                "phoneme_timed_audio_asset_count": phoneme_timed_asset_count,
                "provider_phoneme_timed_audio_asset_count": provider_phoneme_timed_asset_count,
                "estimated_phoneme_timed_audio_asset_count": estimated_phoneme_timed_asset_count,
                "phoneme_timing_missing_count": phoneme_timing_missing_count,
                "issue_count": len(issues),
                "failure_count": fail_count,
                "warning_count": warning_count,
                "issues": issues,
                "selection": self._selection_details(request),
            },
        )

    def _audio_issue(
        self,
        asset: Asset,
        severity: str,
        issue: str,
        details: dict | None = None,
    ) -> dict:
        return {
            "asset_id": str(asset.id),
            "source_entity_id": asset.source_entity_id,
            "severity": severity,
            "issue": issue,
            **(details or {}),
        }

    def _duration_is_plausible(self, actual_ms: int, expected_ms: int) -> bool:
        return max(250, int(expected_ms * 0.35)) <= actual_ms <= max(750, int(expected_ms * 2.5))

    def _expected_sample_rates(self, endpoint: VoiceboxEndpoint | None) -> list[int]:
        if endpoint is None:
            return [48000]
        configured = endpoint.capabilities.get("sample_rates")
        if not configured:
            configured = endpoint.capabilities.get("expected_sample_rates")
        if isinstance(configured, list) and configured:
            return [int(value) for value in configured]
        return [48000]

    def _expected_voice_profile_id(
        self,
        episode: Episode,
        turn: TranscriptTurn,
        profile_by_id: dict[str, VoiceProfile],
    ) -> str | None:
        participant = self._participant_by_id(episode, turn.speaker_participant_id)
        return self._voice_profile_id_for_participant(participant, profile_by_id)

    def _timestamp_issues(self, asset: Asset) -> list[dict]:
        timestamps = asset.generation_metadata.get("word_timestamps")
        if not timestamps:
            return []
        if not isinstance(timestamps, list):
            return [self._audio_issue(asset, "fail", "invalid_word_timestamps")]
        issues: list[dict] = []
        last_end_ms = 0
        for item in timestamps:
            if not isinstance(item, dict):
                issues.append(self._audio_issue(asset, "fail", "invalid_word_timestamp_item"))
                continue
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if (
                start_ms is None
                or end_ms is None
                or int(start_ms) < 0
                or int(end_ms) < int(start_ms)
            ):
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "invalid_word_timestamp_bounds",
                        {"timestamp": item},
                    )
                )
                continue
            last_end_ms = max(last_end_ms, int(end_ms))
        if asset.duration_ms and last_end_ms > asset.duration_ms + 180:
            issues.append(
                self._audio_issue(
                    asset,
                    "warning",
                    "word_timestamp_duration_drift",
                    {
                        "last_word_end_ms": last_end_ms,
                        "duration_ms": asset.duration_ms,
                    },
                )
            )
        return issues

    def _phoneme_timing_issues(self, asset: Asset) -> list[dict]:
        timing = asset.generation_metadata.get("phoneme_timing")
        phonemes = asset.generation_metadata.get("normalized_phoneme_timestamps")
        visemes = asset.generation_metadata.get("viseme_timestamps")
        if not timing:
            return [self._audio_issue(asset, "warning", "missing_phoneme_timing")]
        if not isinstance(timing, dict) or not isinstance(phonemes, list):
            return [self._audio_issue(asset, "fail", "invalid_phoneme_timing")]
        issues: list[dict] = []
        if not phonemes:
            issues.append(self._audio_issue(asset, "warning", "missing_phoneme_timing"))
        if not isinstance(visemes, list):
            issues.append(self._audio_issue(asset, "fail", "invalid_viseme_timing"))
        elif len(visemes) != len(phonemes):
            issues.append(
                self._audio_issue(
                    asset,
                    "fail",
                    "viseme_phoneme_count_mismatch",
                    {"phoneme_count": len(phonemes), "viseme_count": len(visemes)},
                )
            )

        last_start_ms = -1
        for item in phonemes:
            if not isinstance(item, dict):
                issues.append(self._audio_issue(asset, "fail", "invalid_phoneme_item"))
                continue
            phoneme = item.get("phoneme")
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if not phoneme or start_ms is None or end_ms is None:
                issues.append(self._audio_issue(asset, "fail", "invalid_phoneme_item"))
                continue
            try:
                start = int(start_ms)
                end = int(end_ms)
            except (TypeError, ValueError):
                issues.append(self._audio_issue(asset, "fail", "invalid_phoneme_bounds"))
                continue
            if start < 0 or end <= start:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "invalid_phoneme_bounds",
                        {"phoneme": phoneme, "start_ms": start_ms, "end_ms": end_ms},
                    )
                )
                continue
            if start < last_start_ms:
                issues.append(
                    self._audio_issue(
                        asset,
                        "fail",
                        "phoneme_timing_not_monotonic",
                        {"phoneme": phoneme, "start_ms": start},
                    )
                )
            last_start_ms = start
            if asset.duration_ms and end > asset.duration_ms + 180:
                issues.append(
                    self._audio_issue(
                        asset,
                        "warning",
                        "phoneme_duration_drift",
                        {
                            "phoneme": phoneme,
                            "phoneme_end_ms": end,
                            "duration_ms": asset.duration_ms,
                        },
                    )
                )
        return issues

    def _selection_details(
        self,
        request: (
            AudioCancellationRequest
            | AudioGenerationRequest
            | AudioQualityRequest
            | AudioResultSyncRequest
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
            "reset_to_planned": getattr(request, "reset_to_planned", None),
        }

    def _is_selective_generation_request(self, request: AudioGenerationRequest) -> bool:
        return bool(
            request.asset_ids
            or request.transcript_turn_ids
            or request.participant_ids
            or request.failed_only
        )

    def _is_selective_sync_request(self, request: AudioResultSyncRequest) -> bool:
        return bool(
            request.asset_ids
            or request.transcript_turn_ids
            or request.participant_ids
            or request.failed_only
        )

    def _participant_by_id(
        self,
        episode: Episode,
        participant_id: str,
    ) -> ParticipantProfile:
        for participant in episode.participants:
            if participant.id == participant_id:
                return participant
        raise ValueError(f"unknown participant {participant_id}")

    def _voice_profile_id_for_participant(
        self,
        participant: ParticipantProfile,
        profile_by_id: dict[str, VoiceProfile],
    ) -> str | None:
        if participant.voice_profile_id:
            return participant.voice_profile_id
        default_id = f"voice-{participant.id}"
        if default_id in profile_by_id:
            return default_id
        return None
