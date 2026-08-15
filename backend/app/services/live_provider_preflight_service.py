from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from app.domain.schemas import ModelEndpoint, ParticipantProfile, VoiceboxEndpoint, VoiceProfile
from app.infrastructure.repository import EpisodeRepository
from app.services.model_gateway import SecretResolver, openrouter_reasoning_parameters
from app.services.redaction import safe_provider_response_payload

FRONTIER_CAST_PARTICIPANT_IDS = ["chatgpt", "claude", "deepseek", "grok", "gemini", "mistral"]
DEFAULT_PRELIGHT_TEXT = "Guten Tag. DialectiCore prueft jetzt eine echte Stimme fuer den Pilottest."


class LiveProviderPreflightService:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport

    async def run_cast_preflight(
        self,
        repo: EpisodeRepository,
        *,
        participant_ids: list[str] | None = None,
        frontier_cast: bool = True,
        include_models: bool = True,
        include_voices: bool = True,
        text: str = DEFAULT_PRELIGHT_TEXT,
    ) -> dict[str, Any]:
        selected_ids = self._participant_scope(participant_ids or [], frontier_cast=frontier_cast)
        participants = self._selected_participants(repo.list_participant_profiles(), selected_ids)
        model_endpoints = {endpoint.id: endpoint for endpoint in repo.list_model_endpoints()}
        voice_profiles = {profile.id: profile for profile in repo.list_voice_profiles()}
        voicebox_endpoints = {endpoint.id: endpoint for endpoint in repo.list_voicebox_endpoints()}

        result: dict[str, Any] = {
            "schema_version": "live_provider_cast_preflight.v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "participant_scope": {
                "schema_version": "participant_scope.v1",
                "scope": "frontier_cast" if frontier_cast and not participant_ids else "explicit",
                "participant_ids": [participant.id for participant in participants],
            },
        }
        blocking_sections: list[str] = []
        blockers: list[str] = []

        if include_models:
            model_results = await asyncio.gather(
                *(
                    self._participant_model_preflight(
                        participant,
                        model_endpoints.get(participant.model_endpoint_id or ""),
                    )
                    for participant in participants
                )
            )
            model_summary = _participant_summary(
                list(model_results),
                profile_id_key="model_id",
                schema_version="model_participant_preflight_summary.v1",
            )
            result["model_participants"] = list(model_results)
            result["model_summary"] = model_summary
            if model_summary["failed_count"]:
                blocking_sections.append("openrouter")
                blockers.append("one or more selected participants cannot generate model output")

        if include_voices:
            voice_semaphores = {
                endpoint.id: asyncio.Semaphore(max(1, endpoint.max_concurrency))
                for endpoint in voicebox_endpoints.values()
            }
            voice_results = await asyncio.gather(
                *(
                    self._participant_voice_preflight_limited(
                        participant,
                        voice_profiles.get(participant.voice_profile_id or ""),
                        voicebox_endpoints,
                        voice_semaphores,
                        text=text,
                    )
                    for participant in participants
                )
            )
            voice_summary = _participant_summary(
                list(voice_results),
                profile_id_key="voice_profile_id",
                schema_version="voicebox_participant_preflight_summary.v1",
            )
            result["voicebox_participants"] = list(voice_results)
            result["voicebox_summary"] = voice_summary
            if voice_summary["failed_count"]:
                blocking_sections.append("voicebox")
                blockers.append("one or more selected participants cannot generate B1 speech")

        result["status"] = "fail" if blocking_sections else "pass"
        result["blocking_sections"] = blocking_sections
        result["blockers"] = blockers
        return result

    def _participant_scope(self, participant_ids: list[str], *, frontier_cast: bool) -> list[str]:
        if participant_ids:
            return participant_ids
        if frontier_cast:
            return list(FRONTIER_CAST_PARTICIPANT_IDS)
        return []

    def _selected_participants(
        self,
        participants: list[ParticipantProfile],
        participant_ids: list[str],
    ) -> list[ParticipantProfile]:
        enabled = [participant for participant in participants if participant.enabled]
        if not participant_ids:
            return enabled
        by_id = {participant.id: participant for participant in enabled}
        missing = [
            participant_id
            for participant_id in participant_ids
            if participant_id not in by_id
        ]
        if missing:
            raise ValueError(f"enabled participant profile(s) not found: {', '.join(missing)}")
        return [by_id[participant_id] for participant_id in participant_ids]

    async def _participant_voice_preflight_limited(
        self,
        participant: ParticipantProfile,
        profile: VoiceProfile | None,
        endpoints: dict[str, VoiceboxEndpoint],
        semaphores: dict[str, asyncio.Semaphore],
        *,
        text: str,
    ) -> dict[str, Any]:
        endpoint = endpoints.get(profile.voicebox_endpoint_id) if profile else None
        if endpoint is None:
            return await self._participant_voice_preflight(
                participant,
                profile,
                endpoints,
                text=text,
            )
        semaphore = semaphores[endpoint.id]
        async with semaphore:
            return await self._participant_voice_preflight(
                participant,
                profile,
                endpoints,
                text=text,
            )

    async def _participant_model_preflight(
        self,
        participant: ParticipantProfile,
        endpoint: ModelEndpoint | None,
    ) -> dict[str, Any]:
        try:
            if endpoint is None:
                raise ValueError("participant model endpoint was not found")
            smoke = await self._openrouter_smoke(endpoint, participant)
        except Exception as exc:
            smoke = {
                "status": "fail",
                "endpoint_id": participant.model_endpoint_id,
                "model_id": participant.model_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "action": "fix_model_endpoint_or_participant_model_configuration",
            }
            if endpoint is not None:
                smoke["endpoint_name"] = endpoint.name
        return {
            **smoke,
            "participant_id": participant.id,
            "participant_name": participant.display_name,
        }

    async def _openrouter_smoke(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
    ) -> dict[str, Any]:
        if endpoint.id != "openrouter":
            raise ValueError("selected participant is not assigned to the openrouter endpoint")
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if not token:
            raise ValueError("OpenRouter credential reference did not resolve")
        base_url = (endpoint.base_url or "").rstrip("/")
        if not base_url:
            raise ValueError("OpenRouter endpoint has no base_url")
        if not participant.model_id:
            raise ValueError("selected participant has no model_id")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        site_url = str(endpoint.capabilities.get("site_url") or "")
        app_title = str(endpoint.capabilities.get("app_title") or "")
        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_title:
            headers["X-Title"] = app_title
        payload = {
            "model": participant.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": "Reply with one concise German sentence for a live smoke test.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Sprich als {participant.display_name} "
                        "in einem DialectiCore Pilottest."
                    ),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 80,
            **openrouter_reasoning_parameters(endpoint),
        }
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(float(endpoint.default_timeout_seconds)),
        ) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        body = safe_provider_response_payload(response.json())
        content = ""
        if isinstance(body, dict):
            choices = body.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message") if isinstance(choices[0], dict) else {}
                if isinstance(message, dict):
                    content = str(message.get("content") or "")
        result = {
            "status": "pass" if content else "fail",
            "endpoint_id": endpoint.id,
            "endpoint_name": endpoint.name,
            "model_id": participant.model_id,
            "response_chars": len(content),
            "sample": content[:180],
        }
        if not content:
            result["failure_reason"] = "empty_model_response"
            result["action"] = "select_available_model_or_fix_provider_response"
        return result

    async def _participant_voice_preflight(
        self,
        participant: ParticipantProfile,
        profile: VoiceProfile | None,
        endpoints: dict[str, VoiceboxEndpoint],
        *,
        text: str,
    ) -> dict[str, Any]:
        try:
            if profile is None:
                raise ValueError("participant voice profile was not found")
            endpoint = endpoints.get(profile.voicebox_endpoint_id)
            if endpoint is None:
                raise ValueError("participant voicebox endpoint was not found")
            smoke = await self._voicebox_smoke(endpoint, profile, text=text)
        except Exception as exc:
            exception_evidence = _voicebox_exception_evidence(exc)
            smoke = {
                "schema_version": "voicebox_stream_preflight_evidence.v1",
                "status": "fail",
                "voice_profile_id": participant.voice_profile_id,
                "voice_name": profile.name if profile else None,
                "profile_id": profile.voice_id if profile else None,
                "endpoint_id": profile.voicebox_endpoint_id if profile else None,
                "failure_reason": "voice_preflight_exception",
                "error_type": type(exc).__name__,
                "error": exception_evidence["error"],
                "request_url": exception_evidence["request_url"],
                "action": "fix_voice_profile_or_voicebox_endpoint_configuration",
            }
        return {
            **smoke,
            "participant_id": participant.id,
            "participant_name": participant.display_name,
            "model_endpoint_id": participant.model_endpoint_id,
            "model_id": participant.model_id,
        }

    async def _voicebox_smoke(
        self,
        endpoint: VoiceboxEndpoint,
        profile: VoiceProfile,
        *,
        text: str,
    ) -> dict[str, Any]:
        base_url = (endpoint.base_url or "").rstrip("/")
        if not base_url:
            raise ValueError("Voicebox endpoint has no base_url")
        path = str(endpoint.capabilities.get("stream_generation_path") or "/generate/stream")
        accept = str(endpoint.capabilities.get("accept") or "audio/wav")
        prosody = profile.prosody if isinstance(profile.prosody, dict) else {}
        engine = str(
            prosody.get("engine")
            or profile.model_id
            or endpoint.capabilities.get("default_engine")
            or "chatterbox"
        )
        normalize = prosody.get("normalize")
        if not isinstance(normalize, bool):
            normalize = bool(endpoint.capabilities.get("normalize_default", False))
        effects_chain = prosody.get("effects_chain")
        if not isinstance(effects_chain, list):
            effects_chain = endpoint.capabilities.get("effects_chain_default")
        if not isinstance(effects_chain, list):
            effects_chain = []
        if not profile.voice_id:
            raise ValueError("voice profile has no voice_id")
        payload = {
            "profile_id": profile.voice_id,
            "text": text,
            "language": profile.language or "de",
            "engine": engine,
            "normalize": normalize,
            "effects_chain": effects_chain,
        }
        headers = {"Accept": accept, "Content-Type": "application/json"}
        token = self.secret_resolver.resolve(endpoint.credential_reference)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout_seconds = float(
            endpoint.capabilities.get("generation_canary_timeout_seconds") or 90
        )
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(timeout_seconds),
            verify=_endpoint_verify(endpoint),
        ) as client:
            response = await client.post(f"{base_url}{path}", headers=headers, json=payload)
        evidence = _voicebox_response_evidence(
            response=response,
            endpoint=endpoint,
            profile=profile,
            payload=payload,
            path=path,
        )
        if response.status_code >= 400:
            return {
                **evidence,
                "status": "fail",
                "action": "fix_voicebox_generation_then_rerun_health_check",
            }
        if not response.content:
            return {
                **evidence,
                "status": "fail",
                "failure_reason": "empty_audio",
                "action": "fix_voicebox_generation_then_rerun_health_check",
            }
        if not evidence["riff_wave"]:
            return {
                **evidence,
                "status": "fail",
                "failure_reason": "not_riff_wave",
                "action": "fix_voicebox_generation_then_rerun_health_check",
            }
        return {**evidence, "status": "pass"}


def _participant_summary(
    results: list[dict[str, Any]],
    *,
    profile_id_key: str,
    schema_version: str,
) -> dict[str, Any]:
    pass_count = sum(1 for result in results if result.get("status") == "pass")
    failed = [result for result in results if result.get("status") != "pass"]
    return {
        "schema_version": schema_version,
        "participant_count": len(results),
        "pass_count": pass_count,
        "failed_count": len(failed),
        "failed_participant_ids": [
            result.get("participant_id")
            for result in failed
            if isinstance(result.get("participant_id"), str)
        ][:20],
        f"failed_{profile_id_key}s": [
            result.get(profile_id_key)
            for result in failed
            if isinstance(result.get(profile_id_key), str)
        ][:20],
    }


def _endpoint_verify(endpoint: VoiceboxEndpoint) -> bool | str:
    ca_path = str(endpoint.capabilities.get("tls_ca_cert_path") or "").strip()
    if not ca_path:
        return True
    return ca_path if Path(ca_path).is_file() else True


def _voicebox_exception_evidence(exc: Exception) -> dict[str, str | None]:
    message = str(exc).strip()
    request = getattr(exc, "request", None)
    request_url = str(request.url) if isinstance(request, httpx.Request) else None
    if not message:
        message = type(exc).__name__
        if request_url:
            message = f"{message} while requesting {request_url}"
    return {"error": message, "request_url": request_url}


def _voicebox_response_evidence(
    *,
    response: httpx.Response,
    endpoint: VoiceboxEndpoint,
    profile: VoiceProfile,
    payload: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    content = response.content or b""
    return {
        "schema_version": "voicebox_stream_preflight_evidence.v1",
        "endpoint_id": endpoint.id,
        "endpoint_name": endpoint.name,
        "adapter_type": endpoint.adapter_type,
        "url": f"{(endpoint.base_url or '').rstrip('/')}{path}",
        "voice_profile_id": profile.id,
        "voice_name": profile.name,
        "profile_id": payload.get("profile_id"),
        "language": payload.get("language"),
        "engine": payload.get("engine"),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "audio_policy": response.headers.get("x-b1-audio-policy"),
        "audio_sample_rate": response.headers.get("x-b1-audio-sample-rate"),
        "audio_channels": response.headers.get("x-b1-audio-channels"),
        "audio_loudness_lufs": response.headers.get("x-b1-audio-loudness-lufs"),
        "audio_true_peak_dbtp": response.headers.get("x-b1-audio-true-peak-dbtp"),
        "bytes": len(content),
        "riff_wave": content[:12].startswith(b"RIFF") and content[8:12] == b"WAVE",
    }
