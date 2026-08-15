import hashlib
import io
import json
import shutil
import struct
import wave
from pathlib import Path

import httpx
import pytest
from app.core.config import Settings
from app.domain.schemas import (
    Asset,
    AudioAssetPlanRequest,
    AudioCancellationRequest,
    AudioGenerationRequest,
    AudioQualityRequest,
    AudioResultSyncRequest,
    Episode,
    ParticipantProfile,
    TranscriptTurn,
    TranscriptVersion,
    VoiceboxEndpoint,
    VoiceProfile,
)
from app.services.voicebox_service import TtsResult, VoiceboxService
from tests.test_discussion_engine import definition


def pending_review_episode() -> tuple[Episode, TranscriptVersion]:
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="pending_review",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
            speaker_participant_id="host",
            text="Pending transcripts must not enter media production.",
            status="pending_review",
        )
    )
    episode = Episode(
        id=transcript.episode_id,
        title="Pending Audio",
        slug="pending-audio",
        subject="Pending Audio",
        central_question="Should pending transcripts produce audio?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
    )
    return episode, transcript


@pytest.mark.asyncio
async def test_b1_stream_submission_retries_transient_admission_responses() -> None:
    statuses = iter([409, 502, 200])
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        status = next(statuses)
        return httpx.Response(status, headers={"retry-after": "0"})

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.test",
        capabilities={
            "stream_generation_retry_attempts": 3,
            "stream_generation_retry_backoff_seconds": 0,
        },
    )
    async with httpx.AsyncClient(transport=service.transport) as client:
        response, attempts = await service._post_audio_stream_with_retry(
            client=client,
            endpoint=endpoint,
            url="https://voice.test/generate/stream",
            payload={"text": "Guten Tag"},
        )

    assert response.status_code == 200
    assert attempts == 3
    assert len(requests) == 3


def test_audio_planning_rejects_pending_review_transcript() -> None:
    episode, transcript = pending_review_episode()

    with pytest.raises(ValueError, match="transcript must be approved before audio planning"):
        VoiceboxService(Settings()).plan_audio_assets(
            episode,
            AudioAssetPlanRequest(transcript_version_id=transcript.id),
        )


@pytest.mark.asyncio
async def test_spoken_text_qc_transcribes_generated_wav_and_records_pass() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://api.ai.b1.germering/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer b1-token"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'Content-Type: audio/wav' in request.content
        return httpx.Response(
            200,
            json={"text": "Wer die Lasten des Ausbaus am Ende traegt."},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"transcription_base_url": "https://api.ai.b1.germering"},
    )
    result = TtsResult(
        status="completed",
        storage_uri=None,
        mime_type="audio/wav",
        duration_ms=1_000,
        checksum=None,
        metadata={},
        audio_bytes=b"RIFFexampleWAVE",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        verified = await service.verify_spoken_text(
            endpoint,
            result,
            expected_text="Wer die Lasten des Ausbaus am Ende traegt.",
            language="de",
        )

    assert len(requests) == 1
    assert verified.metadata["transcription_qc"] == {
        "status": "passed",
        "passed": True,
        "reason_codes": [],
        "expected_word_count": 8,
        "transcribed_word_count": 8,
        "matching_word_count": 8,
        "expected_script_coverage": 1.0,
        "transcript_precision": 1.0,
        "repeated_phrases": [],
        "provider": "b1_stt",
        "model": "stt-default",
        "expected_text_sha256": hashlib.sha256(
            b"Wer die Lasten des Ausbaus am Ende traegt."
        ).hexdigest(),
        "transcript_sha256": hashlib.sha256(
            b"Wer die Lasten des Ausbaus am Ende traegt."
        ).hexdigest(),
    }


def test_spoken_text_qc_rejects_a_repeated_phrase() -> None:
    expected = "Wer die Lasten des Ausbaus am Ende traegt, muss offen benannt werden."
    spoken = (
        "Wer die Lasten des Ausbaus am Ende traegt, muss offen benannt werden. "
        "Wer die Lasten des Ausbaus am Ende traegt, muss offen benannt werden."
    )

    qc = VoiceboxService._spoken_text_qc(expected, spoken)

    assert qc["passed"] is False
    assert "repeated_spoken_phrase" in qc["reason_codes"]
    assert qc["repeated_phrases"]


@pytest.mark.asyncio
async def test_voicebox_health_probe_reads_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            assert request.headers["authorization"] == "Bearer test-token"
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "capabilities": {
                        "tts": True,
                        "api_key": "leaked-voicebox-health-api-key",
                    },
                },
            )
        if request.url.path == "/capabilities":
            return httpx.Response(
                200,
                json={
                    "word_timestamps": True,
                    "formats": ["audio/wav", "audio/flac"],
                    "nested": {"accessToken": "leaked-voicebox-capability-token"},
                },
            )
        return httpx.Response(404)

    service = VoiceboxService(
        Settings(),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        base_url="https://voicebox.example.test",
        credential_reference="env:VOICEBOX_TEST_TOKEN",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("VOICEBOX_TEST_TOKEN", "test-token")
        checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "healthy"
    assert checked.capabilities == {
        "tts": True,
        "api_key": "[redacted]",
        "word_timestamps": True,
        "formats": ["audio/wav", "audio/flac"],
        "nested": {"accessToken": "[redacted]"},
    }
    capabilities_json = json.dumps(checked.capabilities, sort_keys=True)
    assert "leaked-voicebox-health-api-key" not in capabilities_json
    assert "leaked-voicebox-capability-token" not in capabilities_json


@pytest.mark.asyncio
async def test_voicebox_health_probe_marks_missing_url_unconfigured() -> None:
    service = VoiceboxService(Settings())
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        adapter_type="voicebox_http",
    )

    checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "unconfigured"


@pytest.mark.asyncio
async def test_b1_stream_health_probe_checks_public_ca_without_authorization(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "b1-ai-hub-caddy-root.crt"
    cert_bytes = b"public b1 ca"
    cert_path.write_bytes(cert_bytes)
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/.well-known/b1-ai-hub/caddy-root.crt"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": str(cert_path),
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        checked = await service.check_endpoint_health(endpoint)

    assert len(requests) == 1
    assert checked.health_status == "healthy"
    assert checked.capabilities["credential_reference_resolved"] is True
    assert checked.capabilities["tls_ca_cert_available"] is True
    assert checked.capabilities["ca_cert_bootstrap"]["reachable"] is True
    assert checked.capabilities["ca_cert_bootstrap"]["sha256"] == expected_sha256
    assert checked.capabilities["ca_cert_bootstrap"]["sha256_matches"] is True


@pytest.mark.asyncio
async def test_b1_voice_profile_inventory_uses_native_endpoint_and_filters_invalid_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/profiles"
        assert request.headers["authorization"] == "Bearer b1-token"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "native-de-voice",
                    "name": "B1 profile",
                    "description": "Remote native profile: Erzahler",
                    "language": "de",
                    "default_engine": "chatterbox",
                },
                {"id": "missing-language", "name": "Incomplete"},
                "not-a-profile",
            ],
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        profiles = await service.list_b1_voice_profiles(endpoint)

    assert profiles == [
        {
            "id": "native-de-voice",
            "name": "B1 profile",
            "description": "Remote native profile: Erzahler",
            "language": "de",
            "engine": "chatterbox",
        }
    ]


@pytest.mark.asyncio
async def test_b1_stream_health_probe_clears_stale_credential_error(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "b1-ai-hub-caddy-root.crt"
    cert_bytes = b"public b1 ca"
    cert_path.write_bytes(cert_bytes)
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "credential_reference_error": "credential reference is not available",
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": str(cert_path),
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "healthy"
    assert checked.capabilities["credential_reference_resolved"] is True
    assert "credential_reference_error" not in checked.capabilities


@pytest.mark.asyncio
async def test_b1_stream_health_probe_uses_stored_ca_when_bootstrap_unreachable(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "b1-ai-hub-caddy-root.crt"
    cert_bytes = b"public b1 ca"
    cert_path.write_bytes(cert_bytes)
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS unavailable", request=request)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": str(cert_path),
            "ca_cert_bootstrap": {
                "reachable": True,
                "sha256": expected_sha256,
                "expected_sha256": expected_sha256,
                "sha256_matches": True,
                "stored": True,
            },
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "healthy"
    assert checked.capabilities["credential_reference_resolved"] is True
    assert checked.capabilities["ca_cert_bootstrap"]["stored"] is True
    assert checked.capabilities["ca_cert_bootstrap"]["probe_error"] == "ConnectError"
    assert "ca_cert_bootstrap_probe_error" not in checked.capabilities


@pytest.mark.asyncio
async def test_b1_stream_health_probe_marks_missing_local_ca_unhealthy() -> None:
    cert_bytes = b"public b1 ca"
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": "/missing/b1-ai-hub-caddy-root.crt",
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "unhealthy"
    assert checked.capabilities["tls_ca_cert_available"] is False
    assert checked.capabilities["ca_cert_bootstrap"]["sha256_matches"] is True


@pytest.mark.asyncio
async def test_b1_stream_health_probe_requires_credential_reference(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "b1-ai-hub-caddy-root.crt"
    cert_bytes = b"public b1 ca"
    cert_path.write_bytes(cert_bytes)
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference=None,
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": str(cert_path),
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "unhealthy"
    assert checked.capabilities["credential_reference_configured"] is False
    assert checked.capabilities["credential_reference_resolved"] is False
    assert checked.capabilities["tls_ca_cert_available"] is True
    assert checked.capabilities["ca_cert_bootstrap"]["sha256_matches"] is True


@pytest.mark.asyncio
async def test_b1_stream_health_probe_allows_credential_free_local_bridge() -> None:
    service = VoiceboxService(Settings())
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox-local-bridge",
        name="B1 Voicebox Local Bridge",
        adapter_type="b1_voice_stream",
        base_url="http://127.0.0.1:17493",
        credential_reference=None,
        capabilities={
            "response_mode": "audio_stream",
            "credential_required": False,
            "require_base_url_dns_resolution": True,
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "healthy"
    assert checked.capabilities["credential_reference_configured"] is False
    assert checked.capabilities["credential_reference_resolved"] is False
    assert checked.capabilities["base_url_dns_resolved"] is True


@pytest.mark.asyncio
async def test_b1_stream_health_probe_generation_canary_passes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/generate/stream"
        assert request.headers["accept"] == "audio/wav"
        assert "authorization" not in request.headers
        payload = json.loads(request.content.decode("utf-8"))
        assert payload == {
            "profile_id": "profile-canary",
            "text": "Guten Tag.",
            "language": "de",
            "engine": "chatterbox",
            "normalize": False,
            "effects_chain": [],
        }
        return httpx.Response(
            200,
            content=b"RIFF$\x00\x00\x00WAVEfmt ",
            headers={"content-type": "audio/wav"},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference=None,
        capabilities={
            "credential_required": False,
            "generation_canary_enabled": True,
            "generation_canary_profile_id": "profile-canary",
            "generation_canary_engine": "chatterbox",
            "generation_canary_text": "Guten Tag.",
            "generation_canary_timeout_seconds": 2,
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    assert len(requests) == 1
    assert checked.health_status == "healthy"
    assert checked.capabilities["generation_canary"]["status"] == "pass"
    assert checked.capabilities["generation_canary"]["status_code"] == 200
    assert checked.capabilities["generation_canary"]["riff_wave"] is True
    assert checked.capabilities["generation_canary"]["text_chars"] == len("Guten Tag.")


@pytest.mark.asyncio
async def test_b1_stream_health_probe_retries_scheduler_admission_before_passing() -> None:
    responses = iter(
        [
            httpx.Response(
                409,
                json={"detail": {"message": "GPU scheduler lease is held by another owner"}},
                headers={"retry-after": "0"},
            ),
            httpx.Response(
                200,
                content=b"RIFF$\x00\x00\x00WAVEfmt ",
                headers={"content-type": "audio/wav"},
            ),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference=None,
        capabilities={
            "credential_required": False,
            "generation_canary_enabled": True,
            "generation_canary_profile_id": "profile-canary",
            "stream_generation_retry_attempts": 2,
            "stream_generation_retry_backoff_seconds": 0,
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    canary = checked.capabilities["generation_canary"]
    assert checked.health_status == "healthy"
    assert canary["status"] == "pass"
    assert canary["attempts"] == 2


@pytest.mark.asyncio
async def test_b1_stream_submission_uses_patient_default_retry_budget() -> None:
    responses = iter(
        [
            httpx.Response(409, json={"detail": {"message": "busy"}})
            for _ in range(7)
        ]
        + [httpx.Response(200, content=b"RIFF$\x00\x00\x00WAVEfmt ")]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference=None,
        capabilities={"stream_generation_retry_backoff_seconds": 0},
    )

    async with httpx.AsyncClient(transport=service.transport) as client:
        response, attempts = await service._post_audio_stream_with_retry(
            client=client,
            endpoint=endpoint,
            url="https://voice.ai.b1.germering/generate/stream",
            payload={"text": "Kurz pruefen."},
        )

    assert response.status_code == 200
    assert attempts == 8


@pytest.mark.asyncio
async def test_b1_stream_health_probe_reports_busy_scheduler_as_healthy() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": {"message": "GPU scheduler lease is held by another owner"}},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference=None,
        capabilities={
            "credential_required": False,
            "generation_canary_enabled": True,
            "generation_canary_profile_id": "profile-canary",
            "stream_generation_retry_attempts": 1,
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    canary = checked.capabilities["generation_canary"]
    assert checked.health_status == "healthy"
    assert canary["status"] == "busy"
    assert canary["status_code"] == 409
    assert canary["riff_wave"] is False


@pytest.mark.asyncio
async def test_b1_stream_health_probe_generation_canary_failure_marks_unhealthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/generate/stream"
        return httpx.Response(
            500,
            content=b"Internal Server Error",
            headers={"content-type": "text/plain"},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference=None,
        capabilities={
            "credential_required": False,
            "generation_canary_enabled": True,
            "generation_canary_profile_id": "profile-canary",
            "generation_canary_engine": "chatterbox",
            "generation_canary_timeout_seconds": 2,
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    canary = checked.capabilities["generation_canary"]
    assert checked.health_status == "unhealthy"
    assert canary["status"] == "fail"
    assert canary["status_code"] == 500
    assert canary["content_type"] == "text/plain"
    assert canary["bytes"] == len(b"Internal Server Error")
    assert canary["riff_wave"] is False
    assert "Internal Server Error" not in json.dumps(canary)


@pytest.mark.asyncio
async def test_b1_stream_health_probe_marks_unavailable_credential_unhealthy(
    tmp_path: Path,
) -> None:
    cert_path = tmp_path / "b1-ai-hub-caddy-root.crt"
    cert_bytes = b"public b1 ca"
    cert_path.write_bytes(cert_bytes)
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": str(cert_path),
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("B1_API_KEY", raising=False)
        checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "unhealthy"
    assert checked.capabilities["credential_reference_configured"] is True
    assert checked.capabilities["credential_reference_resolved"] is False
    assert checked.capabilities["credential_reference_error"] == (
        "credential reference is not available"
    )
    assert checked.capabilities["tls_ca_cert_available"] is True
    assert checked.capabilities["ca_cert_bootstrap"]["sha256_matches"] is True


@pytest.mark.asyncio
async def test_b1_stream_ca_bootstrap_stores_certificate_under_runtime_state(
    tmp_path: Path,
) -> None:
    cert_bytes = b"public b1 ca"
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(
        Settings(runtime_state_path=str(tmp_path / "runtime-state")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": "/outside/b1-ai-hub-caddy-root.crt",
        },
    )

    bootstrapped = await service.bootstrap_ca_certificate(endpoint)

    assert len(requests) == 1
    stored_path = Path(bootstrapped.capabilities["tls_ca_cert_path"])
    assert stored_path == tmp_path / "runtime-state" / "certificates" / (
        "b1-ai-hub-caddy-root.crt"
    )
    assert stored_path.read_bytes() == cert_bytes
    assert bootstrapped.capabilities["ca_cert_bootstrap"]["stored"] is True
    assert bootstrapped.capabilities["ca_cert_bootstrap"]["sha256_matches"] is True


@pytest.mark.asyncio
async def test_b1_stream_ca_bootstrap_falls_back_for_path_without_filename(
    tmp_path: Path,
) -> None:
    cert_bytes = b"public b1 ca"
    expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=cert_bytes,
            headers={"x-b1-sha256": expected_sha256},
        )

    service = VoiceboxService(
        Settings(runtime_state_path=str(tmp_path / "runtime-state")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": "/",
        },
    )

    bootstrapped = await service.bootstrap_ca_certificate(endpoint)

    stored_path = Path(bootstrapped.capabilities["tls_ca_cert_path"])
    assert stored_path == tmp_path / "runtime-state" / "certificates" / (
        "b1-voicebox-ca.crt"
    )
    assert stored_path.read_bytes() == cert_bytes


@pytest.mark.asyncio
async def test_b1_stream_ca_bootstrap_rejects_sha_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"different public ca")

    service = VoiceboxService(
        Settings(runtime_state_path=str(tmp_path / "runtime-state")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6",
            "tls_ca_cert_path": "./b1-ai-hub-caddy-root.crt",
        },
    )

    with pytest.raises(ValueError, match="SHA-256"):
        await service.bootstrap_ca_certificate(endpoint)

    assert not (tmp_path / "runtime-state" / "certificates").exists()


@pytest.mark.asyncio
async def test_voicebox_remote_tts_submission_normalizes_result() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tts"
        assert request.headers["authorization"] == "Bearer tts-token"
        payload = json_loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "storage_uri": "s3://dialecticore/audio/turn.wav",
                "mime_type": "audio/wav",
                "duration_ms": 1200,
                "checksum": "sha256:abc",
                "job_id": "job-1",
                "access_token": "leaked-submit-token",
                "accessToken": "leaked-submit-camel-token",
                "nested": {
                    "client_secret": "leaked-submit-secret",
                    "clientSecret": "leaked-submit-camel-secret",
                },
                "word_timestamps": [
                    {"word": "Hello", "start_ms": 0, "end_ms": 400},
                ],
            },
        )

    service = VoiceboxService(
        Settings(),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        base_url="https://voicebox.example.test",
        credential_reference="env:VOICEBOX_TTS_TOKEN",
    )
    profile = VoiceProfile(
        id="voice-host",
        name="Host Voice",
        voicebox_endpoint_id="voicebox-remote",
        voice_id="voice-1",
        language="en",
        rate=1.1,
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
    )
    asset = Asset(
        episode_id=transcript.episode_id,
        asset_type="audio",
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("VOICEBOX_TTS_TOKEN", "tts-token")
        result = await service._submit_tts(endpoint, profile, transcript, turn, asset)

    assert requests[0]["voice"]["voice_id"] == "voice-1"
    assert requests[0]["voice"]["rate"] == 1.1
    assert requests[0]["text"] == "Hello there"
    assert result.status == "completed"
    assert result.storage_uri == "s3://dialecticore/audio/turn.wav"
    assert result.metadata["remote_job_id"] == "job-1"
    assert result.metadata["word_timestamps"][0]["word"] == "Hello"
    assert result.metadata["provider_response"]["access_token"] == "[redacted]"
    assert result.metadata["provider_response"]["accessToken"] == "[redacted]"
    assert result.metadata["provider_response"]["nested"]["client_secret"] == (
        "[redacted]"
    )
    assert result.metadata["provider_response"]["nested"]["clientSecret"] == (
        "[redacted]"
    )
    metadata_json = json.dumps(result.metadata, sort_keys=True)
    assert "leaked-submit-token" not in metadata_json
    assert "leaked-submit-camel-token" not in metadata_json
    assert "leaked-submit-secret" not in metadata_json
    assert "leaked-submit-camel-secret" not in metadata_json


@pytest.mark.asyncio
async def test_voicebox_b1_stream_submission_uses_native_profile_and_stores_wav(
    tmp_path: Path,
) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/generate/stream"
        assert request.headers["authorization"] == "Bearer b1-token"
        assert request.headers["accept"] == "audio/wav"
        payload = json_loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            content=wav_bytes([1200, -1200] * 96_000),
            headers={"content-type": "audio/wav"},
        )

    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "stream_generation_path": "/generate/stream",
            "response_mode": "audio_stream",
            "accept": "audio/wav",
            "default_engine": "chatterbox",
            "normalize_default": False,
            "effects_chain_default": [],
        },
    )
    profile = VoiceProfile(
        id="0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
        name="A_DE_Claude",
        voicebox_endpoint_id="b1-voicebox",
        voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        language="de",
        model_id="chatterbox",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="de",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Guten Tag. Dies ist eine Ausgabe mit A_DE_Claude.",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="B1 Audio",
        slug="b1-audio",
        subject="B1 Audio",
        central_question="How should B1 audio be stored?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id=profile.id,
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="de",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="planned",
            )
        ],
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        generated = await service.generate_audio_assets(
            episode,
            AudioGenerationRequest(language="de", user_id="tester"),
            voicebox_endpoints=[endpoint],
            voice_profiles=[profile],
        )

    assert requests == [
        {
            "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
            "text": "Guten Tag. Dies ist eine Ausgabe mit A_DE_Claude.",
            "language": "de",
            "engine": "chatterbox",
            "normalize": False,
            "effects_chain": [],
        }
    ]
    asset = generated.assets[0]
    assert asset.status == "completed"
    assert asset.storage_uri and asset.storage_uri.startswith("object://dialecticore/audio/")
    assert asset.mime_type == "audio/wav"
    assert asset.checksum
    assert asset.generation_metadata["adapter_type"] == "b1_voice_stream"
    assert asset.generation_metadata["remote_profile_id"] == profile.voice_id
    assert asset.generation_metadata["engine"] == "chatterbox"
    assert asset.generation_metadata["provider_response"]["content_length"] > 0
    assert Path(asset.generation_metadata["object_storage_path"]).exists()
    assert generated.quality_results[-1].status == "warning"


@pytest.mark.asyncio
async def test_voicebox_b1_stream_generation_fails_fast_when_dns_required(
    tmp_path: Path,
) -> None:
    service = VoiceboxService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://definitely-unresolvable.invalid",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "stream_generation_path": "/generate/stream",
            "response_mode": "audio_stream",
            "require_base_url_dns_resolution": True,
        },
    )
    profile = VoiceProfile(
        id="0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
        name="A_DE_Claude",
        voicebox_endpoint_id="b1-voicebox",
        voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        language="de",
        model_id="chatterbox",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="de",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Guten Tag.",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="B1 Audio DNS",
        slug="b1-audio-dns",
        subject="B1 Audio DNS",
        central_question="How should unavailable B1 audio fail?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id=profile.id,
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="de",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="planned",
            )
        ],
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        with pytest.raises(ValueError, match="base URL host could not be resolved"):
            await service.generate_audio_assets(
                episode,
                AudioGenerationRequest(language="de", user_id="tester"),
                voicebox_endpoints=[endpoint],
                voice_profiles=[profile],
            )

    assert episode.assets[0].status == "planned"
    assert episode.assets[0].storage_uri is None


@pytest.mark.asyncio
async def test_voicebox_failed_only_generation_runs_qc_on_targeted_assets(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=wav_bytes([1200, -1200] * 96_000),
            headers={"content-type": "audio/wav"},
        )

    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox-local-bridge",
        name="B1 Voicebox Local Bridge",
        adapter_type="b1_voice_stream",
        base_url="http://127.0.0.1:17493",
        capabilities={
            "stream_generation_path": "/generate/stream",
            "response_mode": "audio_stream",
            "credential_required": False,
        },
    )
    profile = VoiceProfile(
        id="bridge-voice-host",
        name="Bridge Host Voice",
        voicebox_endpoint_id=endpoint.id,
        voice_id="local-host-profile",
        language="de",
        model_id="remote_http",
        prosody={"engine": "remote_http", "normalize": False, "effects_chain": []},
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="de",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Guten Tag.",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Failed Only Audio",
        slug="failed-only-audio",
        subject="Failed Only Audio",
        central_question="How should failed audio retry QC work?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id=profile.id,
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="de",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="failed",
                generation_metadata={"remote_job_id": None},
            )
        ],
    )

    generated = await service.generate_audio_assets(
        episode,
        AudioGenerationRequest(
            transcript_version_id=transcript.id,
            failed_only=True,
            regenerate=True,
            user_id="tester",
        ),
        voicebox_endpoints=[endpoint],
        voice_profiles=[profile],
    )

    assert generated.assets[0].status == "completed"
    assert generated.assets[0].storage_uri
    assert generated.quality_results[-1].check_type == "audio_media_integrity"
    assert generated.quality_results[-1].details["checked_audio_asset_count"] == 1


@pytest.mark.asyncio
async def test_voicebox_remote_tts_completion_downloads_result_url(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/tts":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "audio_url": "https://voicebox.example.test/media/turn.wav",
                    "mime_type": "audio/wav",
                    "job_id": "job-url-1",
                    "sample_rate": 48000,
                    "channels": 1,
                    "detected_language": "en",
                    "word_timestamps": [
                        {"word": "Hello", "start_ms": 0, "end_ms": 400},
                    ],
                },
            )
        if request.url.path == "/media/turn.wav":
            assert request.headers["authorization"] == "Bearer media-token"
            return httpx.Response(
                200,
                content=wav_bytes([5000, -5000] * 24_000),
                headers={"content-type": "audio/wav"},
            )
        return httpx.Response(404)

    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        adapter_type="voicebox_http",
        base_url="https://voicebox.example.test",
        credential_reference="env:VOICEBOX_MEDIA_TOKEN",
        capabilities={"formats": ["audio/wav"], "sample_rates": [48000]},
    )
    profile = VoiceProfile(
        id="voice-host",
        name="Host Voice",
        voicebox_endpoint_id="voicebox-remote",
        voice_id="voice-1",
        language="en",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Remote URL Audio",
        slug="remote-url-audio",
        subject="Remote URL Audio",
        central_question="How should remote media URLs be stored?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="planned",
            )
        ],
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("VOICEBOX_MEDIA_TOKEN", "media-token")
        generated = await service.generate_audio_assets(
            episode,
            AudioGenerationRequest(language="en", user_id="tester"),
            voicebox_endpoints=[endpoint],
            voice_profiles=[profile],
        )

    asset = generated.assets[0]
    probe = asset.generation_metadata["media_probe"]
    assert requests == ["POST /tts", "GET /media/turn.wav"]
    assert asset.status == "completed"
    assert asset.storage_uri and asset.storage_uri.startswith("object://dialecticore/audio/")
    assert asset.generation_metadata["remote_result_uri"] == (
        "https://voicebox.example.test/media/turn.wav"
    )
    assert asset.generation_metadata["remote_result_downloaded"] is True
    assert asset.generation_metadata["storage_backend"] == "local_object_store"
    assert Path(asset.generation_metadata["object_storage_path"]).exists()
    assert probe["duration_ms"] == 1000
    assert probe["peak_dbfs"] is not None
    assert generated.quality_results[-1].check_type == "audio_media_integrity"
    assert generated.quality_results[-1].status == "pass"
    assert generated.quality_results[-1].details["probed_audio_asset_count"] == 1
    assert generated.quality_results[-1].details["downloaded_remote_result_count"] == 1


@pytest.mark.asyncio
async def test_voicebox_async_job_sync_completes_submitted_asset() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/tts":
            return httpx.Response(
                202,
                json={
                    "status": "submitted",
                    "job_id": "job-async-1",
                    "mime_type": "audio/wav",
                },
            )
        if request.url.path == "/tts/jobs/job-async-1":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "job_id": "job-async-1",
                    "storage_uri": "s3://dialecticore/audio/async.wav",
                    "mime_type": "audio/wav",
                    "duration_ms": 1200,
                    "checksum": "sha256:async",
                    "sample_rate": 48000,
                    "channels": 1,
                    "detected_language": "en",
                    "authorization": "Bearer leaked-sync-token",
                    "nested": {"api_key": "leaked-sync-api-key"},
                    "peak_dbfs": -4,
                    "loudness_lufs": -18,
                    "silence_ratio": 0.05,
                    "word_timestamps": [
                        {"word": "Hello", "start_ms": 0, "end_ms": 400},
                        {"word": "there", "start_ms": 401, "end_ms": 900},
                    ],
                    "phoneme_timestamps": [
                        {"phoneme": "h", "start_ms": 0, "end_ms": 120, "word_index": 0},
                        {"phoneme": "vowel", "start_ms": 121, "end_ms": 260, "word_index": 0},
                        {"phoneme": "th", "start_ms": 401, "end_ms": 540, "word_index": 1},
                    ],
                },
            )
        return httpx.Response(404)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        adapter_type="voicebox_http",
        base_url="https://voicebox.example.test",
        capabilities={"formats": ["audio/wav"], "sample_rates": [48000]},
    )
    profile = VoiceProfile(
        id="voice-host",
        name="Host Voice",
        voicebox_endpoint_id="voicebox-remote",
        voice_id="voice-1",
        language="en",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Async Audio",
        slug="async-audio",
        subject="Async Audio",
        central_question="How should remote audio jobs be synchronized?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="planned",
            )
        ],
    )

    submitted = await service.generate_audio_assets(
        episode,
        AudioGenerationRequest(language="en", user_id="tester"),
        voicebox_endpoints=[endpoint],
        voice_profiles=[profile],
    )
    asset = submitted.assets[0]
    assert asset.status == "submitted"
    assert asset.generation_metadata["remote_job_id"] == "job-async-1"
    assert submitted.quality_results[-1].check_type == "audio_media_integrity"
    assert submitted.quality_results[-1].status == "fail"

    synced = await service.sync_audio_results(
        submitted,
        AudioResultSyncRequest(language="en", user_id="tester"),
        voicebox_endpoints=[endpoint],
        voice_profiles=[profile],
    )

    synced_asset = synced.assets[0]
    assert requests == ["POST /tts", "GET /tts/jobs/job-async-1"]
    assert synced_asset.status == "completed"
    assert synced_asset.storage_uri == "s3://dialecticore/audio/async.wav"
    assert synced_asset.checksum == "sha256:async"
    assert synced_asset.generation_metadata["sync_attempt_count"] == 1
    assert synced_asset.generation_metadata["phoneme_timing"]["source"] == (
        "provider_phoneme_timestamps"
    )
    assert synced_asset.generation_metadata["provider_response"]["authorization"] == (
        "[redacted]"
    )
    assert synced_asset.generation_metadata["provider_response"]["nested"]["api_key"] == (
        "[redacted]"
    )
    metadata_json = json.dumps(synced_asset.generation_metadata, sort_keys=True)
    assert "leaked-sync-token" not in metadata_json
    assert "leaked-sync-api-key" not in metadata_json
    assert synced_asset.generation_metadata["phoneme_timing"]["ready_for_lipsync"] is True
    assert synced_asset.generation_metadata["normalized_phoneme_timestamps"][2]["phoneme"] == "th"
    assert synced_asset.generation_metadata["viseme_timestamps"][2]["viseme"] == "tongue_teeth"
    assert synced.quality_results[-1].check_type == "audio_media_integrity"
    assert synced.quality_results[-1].status == "pass"
    assert synced.quality_results[-1].details["provider_phoneme_timed_audio_asset_count"] == 1
    assert synced.audit_events[-1].event_type == "audio.jobs.synced"


@pytest.mark.asyncio
async def test_voicebox_async_job_sync_downloads_completed_result_url(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/tts/jobs/job-url-sync-1":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "job_id": "job-url-sync-1",
                    "result_url": "https://voicebox.example.test/results/sync.wav",
                    "mime_type": "audio/wav",
                    "sample_rate": 48000,
                    "channels": 1,
                    "detected_language": "en",
                    "word_timestamps": [
                        {"word": "Hello", "start_ms": 0, "end_ms": 400},
                        {"word": "there", "start_ms": 401, "end_ms": 900},
                    ],
                },
            )
        if request.url.path == "/results/sync.wav":
            return httpx.Response(
                200,
                content=wav_bytes([5000, -5000] * 24_000),
                headers={"content-type": "audio/wav"},
            )
        return httpx.Response(404)

    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store")),
        transport=httpx.MockTransport(handler),
    )
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        adapter_type="voicebox_http",
        base_url="https://voicebox.example.test",
        capabilities={"formats": ["audio/wav"], "sample_rates": [48000]},
    )
    profile = VoiceProfile(
        id="voice-host",
        name="Host Voice",
        voicebox_endpoint_id="voicebox-remote",
        voice_id="voice-1",
        language="en",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Async URL Audio",
        slug="async-url-audio",
        subject="Async URL Audio",
        central_question="How should remote async result URLs be stored?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="submitted",
                generation_metadata={
                    "voicebox_endpoint_id": "voicebox-remote",
                    "voice_profile_id": "voice-host",
                    "remote_job_id": "job-url-sync-1",
                    "status": "submitted",
                },
            )
        ],
    )

    synced = await service.sync_audio_results(
        episode,
        AudioResultSyncRequest(language="en", user_id="tester"),
        voicebox_endpoints=[endpoint],
        voice_profiles=[profile],
    )

    asset = synced.assets[0]
    assert requests == ["GET /tts/jobs/job-url-sync-1", "GET /results/sync.wav"]
    assert asset.status == "completed"
    assert asset.storage_uri and asset.storage_uri.startswith("object://dialecticore/audio/")
    assert asset.generation_metadata["remote_result_uri"] == (
        "https://voicebox.example.test/results/sync.wav"
    )
    assert asset.generation_metadata["remote_result_downloaded"] is True
    assert asset.generation_metadata["media_probe"]["duration_ms"] == 1000
    assert synced.quality_results[-1].check_type == "audio_media_integrity"
    assert synced.quality_results[-1].status == "pass"
    assert synced.quality_results[-1].details["waveform_analyzed_audio_asset_count"] == 1
    assert synced.quality_results[-1].details["downloaded_remote_result_count"] == 1


@pytest.mark.asyncio
async def test_voicebox_remote_job_cancellation_resets_asset_for_retry() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/tts/jobs/job-cancel-1":
            return httpx.Response(
                200,
                json={
                    "status": "cancelled",
                    "job_id": "job-cancel-1",
                    "token": "leaked-cancel-token",
                    "nested": {"password": "leaked-cancel-password"},
                },
            )
        return httpx.Response(404)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        adapter_type="voicebox_http",
        base_url="https://voicebox.example.test",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Cancel Audio",
        slug="cancel-audio",
        subject="Cancel Audio",
        central_question="How should remote audio jobs be cancelled?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="submitted",
                generation_metadata={
                    "voicebox_endpoint_id": "voicebox-remote",
                    "remote_job_id": "job-cancel-1",
                    "status": "submitted",
                },
            )
        ],
    )

    cancelled = await service.cancel_audio_jobs(
        episode,
        AudioCancellationRequest(language="en", user_id="tester"),
        voicebox_endpoints=[endpoint],
    )

    asset = cancelled.assets[0]
    assert requests == ["DELETE /tts/jobs/job-cancel-1"]
    assert asset.status == "planned"
    assert asset.generation_metadata["remote_job_id"] is None
    assert asset.generation_metadata["cancelled_remote_job_id"] == "job-cancel-1"
    assert asset.generation_metadata["remote_cancel_response"]["token"] == "[redacted]"
    assert asset.generation_metadata["remote_cancel_response"]["nested"]["password"] == (
        "[redacted]"
    )
    metadata_json = json.dumps(asset.generation_metadata, sort_keys=True)
    assert "leaked-cancel-token" not in metadata_json
    assert "leaked-cancel-password" not in metadata_json
    assert asset.generation_metadata["ready_for_retry"] is True
    assert cancelled.audit_events[-1].event_type == "audio.jobs.cancelled"
    assert cancelled.audit_events[-1].details["remote_cancelled_count"] == 1


@pytest.mark.asyncio
async def test_voicebox_cancel_resets_failed_audio_asset_for_retry() -> None:
    service = VoiceboxService(Settings())
    turn = TranscriptTurn(
        transcript_version_id="00000000-0000-0000-0000-000000000010",
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000011"],
        speaker_participant_id="host",
        text="Retry this failed audio.",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000012",
        language="en",
        type="broadcast",
        status="approved",
        turns=[turn],
    )
    episode = Episode(
        id=transcript.episode_id,
        title="Failed Audio Retry",
        slug="failed-audio-retry",
        subject="Failed Audio Retry",
        central_question="Can failed audio be reset for retry?",
        target_duration_seconds=60,
        minimum_duration_seconds=45,
        maximum_duration_seconds=75,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="failed",
                storage_uri="object://dialecticore/audio/failed.wav",
                checksum="sha256:failed",
                generation_metadata={
                    "voicebox_endpoint_id": "voicebox-remote",
                    "remote_job_id": None,
                    "status": "failed",
                    "failure": "provider returned 500",
                },
            )
        ],
    )

    cancelled = await service.cancel_audio_jobs(
        episode,
        AudioCancellationRequest(language="en", failed_only=True, user_id="tester"),
        voicebox_endpoints=[],
    )

    asset = cancelled.assets[0]
    assert asset.status == "planned"
    assert asset.storage_uri is None
    assert asset.checksum is None
    assert asset.generation_metadata["previous_status"] == "failed"
    assert asset.generation_metadata["ready_for_retry"] is True
    assert asset.generation_metadata["failure"] == "provider returned 500"
    assert cancelled.audit_events[-1].event_type == "audio.jobs.cancelled"
    assert cancelled.audit_events[-1].details["cancelled_count"] == 1
    assert cancelled.audit_events[-1].details["remote_skipped_count"] == 1


@pytest.mark.asyncio
async def test_voicebox_cancel_resets_cancelled_audio_asset_for_retry() -> None:
    service = VoiceboxService(Settings())
    turn = TranscriptTurn(
        transcript_version_id="00000000-0000-0000-0000-000000000010",
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000011"],
        speaker_participant_id="host",
        text="Reset this cancelled audio.",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000012",
        language="en",
        type="broadcast",
        status="approved",
        turns=[turn],
    )
    episode = Episode(
        id=transcript.episode_id,
        title="Cancelled Audio Retry",
        slug="cancelled-audio-retry",
        subject="Cancelled Audio Retry",
        central_question="Can cancelled audio be reset for retry?",
        target_duration_seconds=60,
        minimum_duration_seconds=45,
        maximum_duration_seconds=75,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="cancelled",
                generation_metadata={
                    "voice_profile_id": "voice-host",
                    "status": "cancelled",
                    "ready_for_retry": False,
                    "cancelled_remote_job_id": "job-old",
                },
            )
        ],
    )

    reset = await service.cancel_audio_jobs(
        episode,
        AudioCancellationRequest(language="en", user_id="tester", reset_to_planned=True),
        voicebox_endpoints=[],
    )

    asset = reset.assets[0]
    assert asset.status == "planned"
    assert asset.generation_metadata["previous_status"] == "cancelled"
    assert asset.generation_metadata["ready_for_retry"] is True
    assert asset.generation_metadata["cancelled_remote_job_id"] == "job-old"
    assert reset.audit_events[-1].event_type == "audio.jobs.cancelled"
    assert reset.audit_events[-1].details["cancelled_count"] == 1
    assert reset.audit_events[-1].details["remote_skipped_count"] == 1


@pytest.mark.asyncio
async def test_voicebox_regeneration_cancels_running_remote_job_before_retry() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/tts/jobs/job-old/cancel":
            return httpx.Response(
                200,
                json={
                    "status": "cancelled",
                    "job_id": "job-old",
                    "secret": "leaked-regeneration-secret",
                },
            )
        if request.method == "POST" and request.url.path == "/tts":
            return httpx.Response(
                202,
                json={
                    "status": "submitted",
                    "job_id": "job-new",
                    "mime_type": "audio/wav",
                },
            )
        return httpx.Response(404)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="voicebox-remote",
        name="Voicebox Remote",
        adapter_type="voicebox_http",
        base_url="https://voicebox.example.test",
        capabilities={
            "job_cancel_path_template": "/tts/jobs/{job_id}/cancel",
            "job_cancel_method": "POST",
        },
    )
    profile = VoiceProfile(
        id="voice-host",
        name="Host Voice",
        voicebox_endpoint_id="voicebox-remote",
        voice_id="voice-1",
        language="en",
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Retry Audio",
        slug="retry-audio",
        subject="Retry Audio",
        central_question="How should remote audio jobs be retried?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="running",
                generation_metadata={
                    "voicebox_endpoint_id": "voicebox-remote",
                    "voice_profile_id": "voice-host",
                    "remote_job_id": "job-old",
                    "status": "running",
                },
            )
        ],
    )

    retried = await service.generate_audio_assets(
        episode,
        AudioGenerationRequest(language="en", regenerate=True, user_id="tester"),
        voicebox_endpoints=[endpoint],
        voice_profiles=[profile],
    )

    asset = retried.assets[0]
    assert requests == ["POST /tts/jobs/job-old/cancel", "POST /tts"]
    assert asset.status == "submitted"
    assert asset.generation_metadata["remote_job_id"] == "job-new"
    assert asset.generation_metadata["cancelled_remote_job_id"] == "job-old"
    assert asset.generation_metadata["remote_cancel_response"]["secret"] == (
        "[redacted]"
    )
    metadata_json = json.dumps(asset.generation_metadata, sort_keys=True)
    assert "leaked-regeneration-secret" not in metadata_json
    assert asset.generation_metadata["generation_attempt_count"] == 1
    assert retried.audit_events[-2].event_type == "audio.assets.regenerated"


def test_audio_media_qc_fails_invalid_completed_asset_metadata() -> None:
    service = VoiceboxService(Settings())
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Audio QC",
        slug="audio-qc",
        subject="Audio QC",
        central_question="How should audio QC failures be represented?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                storage_uri="mock://voicebox/en/bad.wav",
                mime_type="text/plain",
                duration_ms=120_000,
                checksum="bad",
                status="completed",
                generation_metadata={
                    "voicebox_endpoint_id": "mock-voicebox",
                    "voice_profile_id": "voice-skeptic",
                    "sample_rate": 8000,
                    "detected_language": "de",
                    "peak_dbfs": 0,
                    "silence_ratio": 0.9,
                    "loudness_lufs": -40,
                    "clipping_detected": True,
                    "word_timestamps": [{"word": "Hello", "start_ms": 0, "end_ms": 130_000}],
                },
            )
        ],
    )

    checked = service.run_audio_quality(
        episode,
        AudioQualityRequest(language="en", user_id="tester"),
        voicebox_endpoints=[
            VoiceboxEndpoint(
                id="mock-voicebox",
                name="Mock",
                adapter_type="mock",
                capabilities={"formats": ["audio/wav"], "sample_rates": [48000]},
            )
        ],
        voice_profiles=[
            VoiceProfile(
                id="voice-host",
                name="Host",
                voicebox_endpoint_id="mock-voicebox",
                voice_id="host",
                language="en",
            )
        ],
    )

    qc = checked.quality_results[-1]
    assert qc.check_type == "audio_media_integrity"
    assert qc.status == "fail"
    issue_types = {issue["issue"] for issue in qc.details["issues"]}
    assert {
        "wrong_format",
        "sample_rate_mismatch",
        "wrong_language",
        "inconsistent_voice_profile",
        "clipping_detected",
        "excessive_silence",
        "unexpected_duration",
    } <= issue_types
    assert checked.audit_events[-1].event_type == "audio.qc.completed"


def test_audio_media_qc_prefers_waveform_probe_over_provider_hints(tmp_path: Path) -> None:
    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    clipped_turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    silent_turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000003"],
        speaker_participant_id="host",
        text="Another short turn",
        status="accepted",
    )
    transcript.turns.extend([clipped_turn, silent_turn])
    clipped_object = service.object_store.put_bytes(
        "audio/test/clipped.wav",
        wav_bytes([32767] * 48_000),
        "audio/wav",
    )
    silent_object = service.object_store.put_bytes(
        "audio/test/silent.wav",
        wav_bytes([0] * 48_000),
        "audio/wav",
    )
    episode = Episode(
        id=transcript.episode_id,
        title="Waveform QC",
        slug="waveform-qc",
        subject="Waveform QC",
        central_question="How should waveform defects be represented?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(clipped_turn.id),
                storage_uri=clipped_object.uri,
                mime_type="audio/wav",
                duration_ms=1000,
                checksum=clipped_object.checksum,
                status="completed",
                generation_metadata={
                    "voicebox_endpoint_id": "mock-voicebox",
                    "voice_profile_id": "voice-host",
                    "sample_rate": 48000,
                    "detected_language": "en",
                    "peak_dbfs": -12,
                    "silence_ratio": 0.0,
                    "loudness_lufs": -18,
                    "clipping_detected": False,
                },
            ),
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(silent_turn.id),
                storage_uri=silent_object.uri,
                mime_type="audio/wav",
                duration_ms=1000,
                checksum=silent_object.checksum,
                status="completed",
                generation_metadata={
                    "voicebox_endpoint_id": "mock-voicebox",
                    "voice_profile_id": "voice-host",
                    "sample_rate": 48000,
                    "detected_language": "en",
                    "peak_dbfs": -12,
                    "silence_ratio": 0.0,
                    "loudness_lufs": -18,
                    "clipping_detected": False,
                },
            ),
        ],
    )

    checked = service.run_audio_quality(
        episode,
        AudioQualityRequest(language="en", user_id="tester"),
        voicebox_endpoints=[
            VoiceboxEndpoint(
                id="mock-voicebox",
                name="Mock",
                adapter_type="mock",
                capabilities={"formats": ["audio/wav"], "sample_rates": [48000]},
            )
        ],
        voice_profiles=[
            VoiceProfile(
                id="voice-host",
                name="Host",
                voicebox_endpoint_id="mock-voicebox",
                voice_id="host",
                language="en",
            )
        ],
    )

    qc = checked.quality_results[-1]
    assert qc.status == "fail"
    assert qc.details["probed_audio_asset_count"] == 2
    assert qc.details["waveform_analyzed_audio_asset_count"] == 2
    issues = qc.details["issues"]
    issue_types = {issue["issue"] for issue in issues}
    assert {"clipping_detected", "clipping_risk", "excessive_silence"} <= issue_types
    assert {
        issue["source"]
        for issue in issues
        if issue["issue"] in {"clipping_detected", "clipping_risk", "excessive_silence"}
    } == {"media_probe"}


def test_audio_media_qc_accepts_expected_sample_rates_capability(tmp_path: Path) -> None:
    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="de",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Guten Tag",
        status="accepted",
    )
    transcript.turns.append(turn)
    stored = service.object_store.put_bytes(
        "audio/test/bridge-24khz.wav",
        wav_bytes([1200] * 24_000, sample_rate=24_000),
        "audio/wav",
    )
    episode = Episode(
        id=transcript.episode_id,
        title="Bridge QC",
        slug="bridge-qc",
        subject="Bridge QC",
        central_question="Can bridge audio pass sample-rate QC?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="de",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                storage_uri=stored.uri,
                mime_type="audio/wav",
                duration_ms=1000,
                checksum=stored.checksum,
                status="completed",
                generation_metadata={
                    "voicebox_endpoint_id": "bridge",
                    "voice_profile_id": "voice-host",
                    "detected_language": "de",
                },
            )
        ],
    )

    checked = service.run_audio_quality(
        episode,
        AudioQualityRequest(language="de", user_id="tester"),
        voicebox_endpoints=[
            VoiceboxEndpoint(
                id="bridge",
                name="Bridge",
                adapter_type="b1_voice_stream",
                capabilities={"formats": ["audio/wav"], "expected_sample_rates": [24000]},
            )
        ],
        voice_profiles=[
            VoiceProfile(
                id="voice-host",
                name="Host",
                voicebox_endpoint_id="bridge",
                voice_id="host",
                language="de",
            )
        ],
    )

    issues = checked.quality_results[-1].details["issues"]
    assert "sample_rate_mismatch" not in {issue["issue"] for issue in issues}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
@pytest.mark.asyncio
async def test_materialized_b1_audio_can_be_postprocessed_for_clipping(
    tmp_path: Path,
) -> None:
    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    endpoint = VoiceboxEndpoint(
        id="bridge",
        name="Bridge",
        adapter_type="b1_voice_stream",
        capabilities={
            "postprocess_audio_loudness": True,
            "expected_sample_rates": [24000],
        },
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="de",
        status="approved",
    )
    asset = Asset(
        episode_id=transcript.episode_id,
        asset_type="audio",
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id="00000000-0000-0000-0000-000000000002",
        status="submitted",
    )
    result = TtsResult(
        status="completed",
        storage_uri=None,
        mime_type="audio/wav",
        duration_ms=None,
        checksum=None,
        metadata={},
        audio_bytes=wav_bytes([32767, -32768] * 24_000, sample_rate=24_000),
    )

    materialized = await service._materialize_audio_result(
        endpoint,
        transcript,
        asset,
        result,
    )

    assert materialized.metadata["audio_postprocess_applied"] is True
    probe = materialized.metadata["media_probe"]
    assert probe["sample_rate"] == 24000
    assert probe["clipping_detected"] is False
    assert probe["peak_dbfs"] < -0.1


@pytest.mark.asyncio
async def test_audio_postprocess_falls_back_when_ffmpeg_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    monkeypatch.setattr("app.services.voicebox_service.shutil.which", lambda _: None)
    payload = wav_bytes([1200, -1200] * 24_000, sample_rate=24_000)
    endpoint = VoiceboxEndpoint(
        id="bridge",
        name="Bridge",
        adapter_type="b1_voice_stream",
        capabilities={"postprocess_audio_loudness": True},
    )

    processed, metadata = service._postprocess_audio_bytes(
        endpoint=endpoint,
        payload=payload,
        mime_type="audio/wav",
        extension="wav",
    )

    assert processed == payload
    assert metadata == {
        "audio_postprocess_applied": False,
        "audio_postprocess_skipped": "ffmpeg_unavailable",
        "delivery_rate": 1.0,
        "delivery_rate_applied": False,
    }


@pytest.mark.asyncio
async def test_b1_voice_preview_applies_pitch_preserving_delivery_rate() -> None:
    source = wav_bytes([500, -500] * 24_000)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/generate/stream"
        return httpx.Response(200, content=source, headers={"content-type": "audio/wav"})

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.test",
    )
    profile = VoiceProfile(
        id="preview-voice",
        name="Preview Voice",
        voicebox_endpoint_id="b1-voicebox",
        voice_id="remote-voice",
        language="de",
        rate=0.8,
    )

    preview, mime_type = await service.render_voice_preview(
        endpoint,
        profile,
        "Kurz und klar.",
    )

    with wave.open(io.BytesIO(preview), "rb") as audio:
        duration_ms = round(audio.getnframes() / audio.getframerate() * 1000)
    assert mime_type == "audio/wav"
    assert 1_150 <= duration_ms <= 1_350


def test_delivery_rate_scales_provider_timing_for_the_final_wav() -> None:
    service = VoiceboxService(Settings())
    metadata = {
        "word_timestamps": [{"word": "Hallo", "start_ms": 100, "end_ms": 600}],
        "phoneme_timestamps": [{"phoneme": "a", "start_ms": 100, "end_ms": 300}],
        "character_timestamps": [{"character": "H", "start_ms": 100, "end_ms": 200}],
        "word_timing": {"duration_ms": 1_000, "source": "b1_ctc_forced_alignment"},
        "b1_timing": {"audio_sha256": "a" * 64, "checksum_bound": True},
    }

    scaled = service._apply_delivery_rate_timing(
        metadata,
        {"delivery_rate": 0.8, "delivery_rate_applied": True},
        duration_ms=1_250,
        checksum="b" * 64,
    )

    assert scaled["word_timestamps"][0]["start_ms"] == 125
    assert scaled["word_timestamps"][0]["end_ms"] == 750
    assert scaled["phoneme_timestamps"][0]["end_ms"] == 375
    assert scaled["character_timestamps"][0]["end_ms"] == 250
    assert scaled["word_timing"]["duration_ms"] == 1_250
    assert scaled["b1_timing"]["source_audio_sha256"] == "a" * 64
    assert scaled["b1_timing"]["audio_sha256"] == "b" * 64
    assert scaled["b1_timing"]["checksum_bound"] is False


def test_b1_audio_without_provider_timestamps_gets_estimated_lipsync_timing(
    tmp_path: Path,
) -> None:
    service = VoiceboxService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    endpoint = VoiceboxEndpoint(
        id="bridge",
        name="Bridge",
        adapter_type="b1_voice_stream",
        capabilities={"estimate_word_timestamps_from_text": True},
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Guten Tag zusammen",
        status="accepted",
    )
    asset = Asset(
        episode_id="00000000-0000-0000-0000-000000000001",
        asset_type="audio",
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        duration_ms=1500,
        status="completed",
    )
    result = TtsResult(
        status="completed",
        storage_uri="object://dialecticore/audio/test.wav",
        mime_type="audio/wav",
        duration_ms=1500,
        checksum="sha256:test",
        metadata={"word_timestamps": [], "phoneme_timestamps": []},
    )

    timed = service._with_timing_tracks(endpoint, turn, asset, result)

    assert timed.metadata["word_timing"] == {
        "source": "estimated_from_transcript_text",
        "confidence": 0.35,
        "word_count": 3,
        "duration_ms": 1500,
    }
    assert timed.metadata["phoneme_timing"]["source"] == "estimated_from_transcript_text"
    assert timed.metadata["phoneme_timing"]["ready_for_lipsync"] is True
    assert timed.metadata["normalized_phoneme_timestamps"]
    assert timed.metadata["viseme_timestamps"]


@pytest.mark.asyncio
async def test_b1_stream_uses_checksum_bound_forced_alignment_timing() -> None:
    audio = wav_bytes([500, -500] * 24_000)
    checksum = hashlib.sha256(audio).hexdigest()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/generate/stream":
            return httpx.Response(
                200,
                content=audio,
                headers={
                    "content-type": "audio/wav",
                    "x-b1-generation-id": "generation-1",
                    "x-b1-audio-sha256": checksum,
                    "x-b1-timing-url": "/generate/timing/generation-1",
                },
            )
        if request.url.path == "/generate/timing/generation-1":
            return httpx.Response(
                200,
                json={
                    "schema_version": "b1_voice_timing.v1",
                    "generation_id": "generation-1",
                    "audio_sha256": checksum,
                    "duration_ms": 1000,
                    "timing_method": "mms_fa_ctc_forced_alignment",
                    "timing_precision": "word_ctc_phoneme_word_window",
                    "phoneme_alphabet": "ipa",
                    "alignment_model": "MMS_FA",
                    "alignment_device": "cuda",
                    "word_timestamps": [
                        {"word": "Guten", "start_ms": 10, "end_ms": 400, "confidence": 0.98},
                        {"word": "Tag", "start_ms": 410, "end_ms": 800, "confidence": 0.97},
                    ],
                    "character_timestamps": [
                        {"character": "G", "start_ms": 10, "end_ms": 90, "word_index": 0}
                    ],
                    "phoneme_timestamps": [
                        {"phoneme": "g", "start_ms": 10, "end_ms": 120, "word_index": 0},
                        {"phoneme": "u", "start_ms": 120, "end_ms": 280, "word_index": 0},
                    ],
                },
            )
        return httpx.Response(404)

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.test",
        credential_reference="env:B1_API_KEY",
    )
    profile = VoiceProfile(
        id="voice-host",
        name="Host Voice",
        voicebox_endpoint_id="b1-voicebox",
        voice_id="voice-1",
        language="de",
        prosody={"engine": "chatterbox", "normalize": True, "effects_chain": []},
    )
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="de",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Guten Tag",
        status="accepted",
    )
    asset = Asset(
        episode_id=transcript.episode_id,
        asset_type="audio",
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        duration_ms=1000,
        status="planned",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("B1_API_KEY", "test-token")
        result = await service._submit_audio_stream_tts(endpoint, profile, transcript, turn, asset)
    timed = service._with_timing_tracks(endpoint, turn, asset, result)

    assert requests == ["POST /generate/stream", "GET /generate/timing/generation-1"]
    assert timed.metadata["b1_timing"]["status"] == "completed"
    assert timed.metadata["b1_timing"]["checksum_bound"] is True
    assert timed.metadata["word_timing"]["source"] == "b1_ctc_forced_alignment"
    assert timed.metadata["phoneme_timing"]["source"] == "b1_ipa_from_ctc_word_windows"
    assert timed.metadata["phoneme_timing"]["ready_for_lipsync"] is True
    assert timed.metadata["viseme_timestamps"][1]["viseme"] == "rounded"


@pytest.mark.asyncio
async def test_b1_stream_rejects_timing_with_mismatched_audio_checksum() -> None:
    audio = wav_bytes([500, -500] * 24_000)
    checksum = hashlib.sha256(audio).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/generate/stream":
            return httpx.Response(
                200,
                content=audio,
                headers={
                    "content-type": "audio/wav",
                    "x-b1-generation-id": "generation-1",
                    "x-b1-audio-sha256": checksum,
                    "x-b1-timing-url": "/generate/timing/generation-1",
                },
            )
        return httpx.Response(
            200,
            json={
                "generation_id": "generation-1",
                "audio_sha256": "0" * 64,
                "word_timestamps": [],
                "phoneme_timestamps": [],
            },
        )

    service = VoiceboxService(Settings(), transport=httpx.MockTransport(handler))
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.test",
    )
    response = httpx.Response(
        200,
        content=audio,
        headers={
            "x-b1-generation-id": "generation-1",
            "x-b1-audio-sha256": checksum,
            "x-b1-timing-url": "/generate/timing/generation-1",
        },
        request=httpx.Request("POST", "https://voice.test/generate/stream"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        timing = await service._fetch_b1_stream_timing(
            client=client,
            endpoint=endpoint,
            response=response,
        )

    assert timing == {
        "b1_timing": {
            "status": "invalid",
            "reason": "timing_binding_mismatch",
            "generation_id": "generation-1",
        }
    }


def json_loads(content: bytes) -> dict:
    import json

    return json.loads(content)


def wav_bytes(samples: list[int], sample_rate: int = 48000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()
