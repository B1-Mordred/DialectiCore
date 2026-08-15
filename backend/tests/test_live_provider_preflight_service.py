from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from app.api.routes import get_live_provider_preflight_service, get_repository
from app.domain.schemas import VoiceboxEndpoint
from app.infrastructure.repository import EpisodeRepository
from app.main import app
from app.services.live_provider_preflight_service import LiveProviderPreflightService
from fastapi.testclient import TestClient


class FixedSecretResolver:
    def resolve(self, credential_reference: str | None) -> str | None:
        return "test-token" if credential_reference else None


class FakeLiveProviderPreflightService:
    def __init__(self):
        self.calls: list[dict] = []

    async def run_cast_preflight(self, repo, **kwargs):
        self.calls.append(kwargs)
        return {
            "schema_version": "live_provider_cast_preflight.v1",
            "status": "pass",
            "participant_scope": {
                "schema_version": "participant_scope.v1",
                "scope": "frontier_cast",
                "participant_ids": ["chatgpt", "claude"],
            },
            "blocking_sections": [],
            "model_summary": {
                "schema_version": "model_participant_preflight_summary.v1",
                "participant_count": 2,
                "pass_count": 2,
                "failed_count": 0,
            },
            "voicebox_summary": {
                "schema_version": "voicebox_participant_preflight_summary.v1",
                "participant_count": 2,
                "pass_count": 2,
                "failed_count": 0,
            },
        }


def test_live_provider_preflight_route_records_audit_event():
    repository = EpisodeRepository()
    service = FakeLiveProviderPreflightService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_live_provider_preflight_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.post(
            "/api/v1/system/live-provider-preflight",
            json={"frontier_cast": True, "user_id": "operator"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "pass"
        assert payload["participant_scope"]["participant_ids"] == ["chatgpt", "claude"]

        events = repository.list_audit_events(limit=5)
        assert events[0].event_type == "live_provider.cast_preflight_checked"
        assert events[0].actor == "operator"
        assert events[0].details["status"] == "pass"
        assert events[0].details["participant_ids"] == ["chatgpt", "claude"]
        assert service.calls[0]["frontier_cast"] is True
        assert service.calls[0]["include_models"] is True
        assert service.calls[0]["include_voices"] is True
    finally:
        app.dependency_overrides.clear()


def test_live_provider_preflight_route_accepts_empty_post_body_defaults():
    repository = EpisodeRepository()
    service = FakeLiveProviderPreflightService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_live_provider_preflight_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.post("/api/v1/system/live-provider-preflight")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "pass"
        assert service.calls == [
            {
                "participant_ids": [],
                "frontier_cast": True,
                "include_models": True,
                "include_voices": True,
                "text": (
                    "Guten Tag. DialectiCore prueft jetzt eine echte Stimme "
                    "fuer den Pilottest."
                ),
            }
        ]
        events = repository.list_audit_events(limit=5)
        assert events[0].event_type == "live_provider.cast_preflight_checked"
        assert events[0].actor == "web-ui"
    finally:
        app.dependency_overrides.clear()


def test_live_provider_preflight_get_runs_default_frontier_cast():
    repository = EpisodeRepository()
    service = FakeLiveProviderPreflightService()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_live_provider_preflight_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.get("/api/v1/system/live-provider-preflight")
        assert response.status_code == 200
        payload = response.json()
        assert payload["participant_scope"]["scope"] == "frontier_cast"
        assert service.calls[0]["participant_ids"] == []
        assert service.calls[0]["frontier_cast"] is True
        assert service.calls[0]["include_models"] is True
        assert service.calls[0]["include_voices"] is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cast_preflight_passes_when_models_and_voices_return_valid_payloads():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Bereit fuer den Pilottest."}}]},
            )
        if request.url.host == "voice.test":
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=b"RIFF\x24\x00\x00\x00WAVEfmt ",
            )
        return httpx.Response(404)

    repository = _repository_with_live_cast()
    service = LiveProviderPreflightService(
        secret_resolver=FixedSecretResolver(),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_cast_preflight(repository)

    assert result["status"] == "pass"
    assert result["model_summary"]["participant_count"] == 6
    assert result["model_summary"]["failed_count"] == 0
    assert result["voicebox_summary"]["participant_count"] == 6
    assert result["voicebox_summary"]["failed_count"] == 0
    assert {request.url.host for request in requests} == {"openrouter.ai", "voice.test"}


@pytest.mark.asyncio
async def test_voicebox_preflight_uses_profile_broadcast_prosody_and_records_headers():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/wav",
                "x-b1-audio-policy": "broadcast",
                "x-b1-audio-sample-rate": "48000",
                "x-b1-audio-channels": "1",
                "x-b1-audio-loudness-lufs": "-18.0",
                "x-b1-audio-true-peak-dbtp": "-1.5",
            },
            content=b"RIFF\x24\x00\x00\x00WAVEfmt ",
        )

    repository = _repository_with_live_cast()
    profile = repository.get_voice_profile("0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5")
    repository.upsert_voice_profile(
        profile.model_copy(
            update={
                "prosody": {"engine": "chatterbox", "normalize": True, "effects_chain": []}
            }
        )
    )
    service = LiveProviderPreflightService(
        secret_resolver=FixedSecretResolver(),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_cast_preflight(
        repository,
        participant_ids=["claude"],
        include_models=False,
    )

    assert result["status"] == "pass"
    assert json.loads(requests[0].content)["normalize"] is True
    voice = result["voicebox_participants"][0]
    assert voice["audio_policy"] == "broadcast"
    assert voice["audio_sample_rate"] == "48000"
    assert voice["audio_true_peak_dbtp"] == "-1.5"


@pytest.mark.asyncio
async def test_cast_preflight_blocks_when_voicebox_returns_non_wav_payloads():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Modell bereit."}}]},
            )
        return httpx.Response(
            500,
            headers={"content-type": "text/plain"},
            content=b"internal error",
        )

    repository = _repository_with_live_cast()
    service = LiveProviderPreflightService(
        secret_resolver=FixedSecretResolver(),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_cast_preflight(repository)

    assert result["status"] == "fail"
    assert result["blocking_sections"] == ["voicebox"]
    assert result["model_summary"]["failed_count"] == 0
    assert result["voicebox_summary"]["failed_count"] == 6
    assert result["voicebox_summary"]["failed_participant_ids"] == [
        "chatgpt",
        "claude",
        "deepseek",
        "grok",
        "gemini",
        "mistral",
    ]


@pytest.mark.asyncio
async def test_cast_preflight_reports_empty_voicebox_request_errors_with_request_url():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Modell bereit."}}]},
            )
        raise httpx.ConnectError("", request=request)

    repository = _repository_with_live_cast()
    service = LiveProviderPreflightService(
        secret_resolver=FixedSecretResolver(),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_cast_preflight(repository)

    assert result["status"] == "fail"
    assert result["blocking_sections"] == ["voicebox"]
    voice = result["voicebox_participants"][0]
    assert voice["error_type"] == "ConnectError"
    assert voice["request_url"] == "https://voice.test/generate/stream"
    assert voice["error"] == "ConnectError while requesting https://voice.test/generate/stream"
    assert "test-token" not in voice["error"]


@pytest.mark.asyncio
async def test_cast_preflight_runs_participant_checks_concurrently():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Bereit."}}]},
            )
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=b"RIFF\x24\x00\x00\x00WAVEfmt ",
        )

    repository = _repository_with_live_cast()
    service = LiveProviderPreflightService(
        secret_resolver=FixedSecretResolver(),
        transport=httpx.MockTransport(handler),
    )

    started_at = time.perf_counter()
    result = await service.run_cast_preflight(repository)
    elapsed = time.perf_counter() - started_at

    assert result["status"] == "pass"
    assert result["model_summary"]["participant_count"] == 6
    assert result["voicebox_summary"]["participant_count"] == 6
    assert elapsed < 0.8


@pytest.mark.asyncio
async def test_cast_preflight_respects_voicebox_endpoint_concurrency_limit():
    active_voice_requests = 0
    peak_voice_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_voice_requests, peak_voice_requests
        if request.url.host == "voice.test":
            active_voice_requests += 1
            peak_voice_requests = max(peak_voice_requests, active_voice_requests)
            await asyncio.sleep(0.01)
            active_voice_requests -= 1
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=b"RIFF\x24\x00\x00\x00WAVEfmt ",
            )
        return httpx.Response(404)

    repository = _repository_with_live_cast()
    endpoint = repository.get_voicebox_endpoint("b1-voicebox")
    repository.upsert_voicebox_endpoint(endpoint.model_copy(update={"max_concurrency": 1}))
    service = LiveProviderPreflightService(
        secret_resolver=FixedSecretResolver(),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_cast_preflight(repository, include_models=False)

    assert result["status"] == "pass"
    assert result["voicebox_summary"]["pass_count"] == 6
    assert peak_voice_requests == 1


def _repository_with_live_cast() -> EpisodeRepository:
    repository = EpisodeRepository()
    repository.provision_openrouter_presets(assign_participants=True)
    repository.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="b1-voicebox",
            name="B1 Voicebox",
            adapter_type="b1_voice_stream",
            base_url="https://voice.test",
            credential_reference="env:B1_API_KEY",
            capabilities={
                "stream_generation_path": "/generate/stream",
                "accept": "audio/wav",
                "default_engine": "chatterbox",
                "normalize_default": False,
                "effects_chain_default": [],
            },
            health_status="healthy",
        )
    )
    repository.provision_b1_german_voice_presets(
        endpoint_id="b1-voicebox",
        assign_participants=True,
        reassign_participants=True,
    )
    return repository
