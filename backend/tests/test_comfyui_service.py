import base64
import hashlib
import io
import json
import subprocess
import wave
import zlib
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import app.services.comfyui_service as comfyui_module
import httpx
import pytest
from app.core.config import Settings
from app.domain.defaults import (
    default_comfyui_endpoints,
    default_comfyui_workflows,
    default_model_endpoints,
    default_participants,
    default_visual_profiles,
)
from app.domain.enums import AssetType, EpisodeStatus, TranscriptType
from app.domain.schemas import (
    Asset,
    Claim,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    Episode,
    EpisodeCreateRequest,
    SeatedCharacterReviewRequest,
    StudioPanelReviewRequest,
    TimelineBuildRequest,
    TranscriptTurn,
    TranscriptVersion,
    VisualAssetPlanRequest,
    VisualCancellationRequest,
    VisualGenerationRequest,
    VisualQualityRequest,
    VisualReferenceImage,
    VisualResultSyncRequest,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.comfyui_service import ComfyUiService, VisualResult
from app.services.discussion_engine import DiscussionEngine
from app.services.model_gateway import ModelGateway
from app.services.object_storage import create_object_store
from app.services.timeline_service import TimelineService
from tests.test_discussion_engine import definition


def approve_canonical_transcript(episode: Episode) -> Episode:
    transcript = next(
        transcript
        for transcript in episode.transcripts
        if transcript.id == episode.canonical_transcript_version_id
    )
    transcript.status = "approved"
    for turn in transcript.turns:
        if turn.status != "excluded":
            turn.status = "accepted"
    return episode


def png_rgba(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    def chunk(chunk_type: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(chunk_type)
        crc = zlib.crc32(payload, crc)
        return (
            len(payload).to_bytes(4, "big")
            + chunk_type
            + payload
            + crc.to_bytes(4, "big")
        )

    rows = []
    for row_index in range(height):
        row = bytearray([0])
        for column_index in range(width):
            row.extend(pixels[(row_index * width) + column_index])
        rows.append(bytes(row))
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 6, 0, 0, 0])
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def test_video_probe_records_codec_fps_and_estimated_frame_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")
    monkeypatch.setattr(comfyui_module.shutil, "which", lambda name: "/usr/bin/ffprobe")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                            "r_frame_rate": "30/1",
                            "pix_fmt": "yuv420p",
                        }
                    ],
                    "format": {
                        "duration": "2.000",
                        "size": "4096",
                        "bit_rate": "16384",
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    },
                }
            ),
        )

    monkeypatch.setattr(comfyui_module.subprocess, "run", fake_run)

    probe = ComfyUiService()._probe_video_path(video_path, "video/mp4")

    assert probe.render_ready is True
    assert probe.width == 1920
    assert probe.height == 1080
    assert probe.duration_ms == 2000
    assert probe.fps == 30
    assert probe.frame_count == 60
    assert probe.video_analysis
    assert probe.video_analysis["codec_name"] == "h264"
    assert probe.video_analysis["pixel_format"] == "yuv420p"
    assert probe.video_analysis["frame_count_source"] == "estimated_from_duration_and_fps"
    assert "video probe missing exact frame count" in probe.probe_warnings


async def planned_visual_episode(settings: Settings) -> Episode:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), settings).run(episode)
    approve_canonical_transcript(produced)
    return ComfyUiService(settings).plan_visual_assets(
        produced,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
    )


def prepare_b1_lipsync_inputs(settings: Settings, episode: Episode, visual: Asset) -> Asset:
    object_store = create_object_store(settings)
    portrait = png_rgba(1, 1, [(40, 90, 180, 255)])
    portrait_object = object_store.put_bytes(
        f"visual-profiles/{visual.id}/portrait.png",
        portrait,
        "image/png",
    )
    prompt_inputs = dict(visual.generation_metadata.get("prompt_inputs") or {})
    prompt_inputs.update(
        {
            "reference_image_uri": portrait_object.uri,
            "portrait_reference_image_uri": portrait_object.uri,
        }
    )
    visual.generation_metadata["prompt_inputs"] = prompt_inputs

    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x00\x00" * 8000)
    audio_object = object_store.put_bytes(
        f"audio/{visual.id}.wav",
        audio_buffer.getvalue(),
        "audio/wav",
    )
    audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language=visual.language,
        source_entity_type="transcript_turn",
        source_entity_id=visual.source_entity_id,
        storage_uri=audio_object.uri,
        mime_type="audio/wav",
        duration_ms=1000,
        checksum=audio_object.checksum,
        generation_metadata={
            "transcript_version_id": visual.generation_metadata["transcript_version_id"]
        },
        status="completed",
    )
    episode.assets.append(audio)
    return audio


def test_streamed_wav_is_finalized_for_b1_without_changing_pcm_bytes() -> None:
    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x01\x02" * 32)
    streamed = bytearray(audio_buffer.getvalue())
    data_offset = streamed.index(b"data")
    streamed[4:8] = b"\xff\xff\xff\xff"
    streamed[data_offset + 4 : data_offset + 8] = b"\xff\xff\xff\xff"

    finalized = ComfyUiService()._finalize_streamed_wav_for_b1(bytes(streamed))

    assert finalized[:4] == b"RIFF"
    assert int.from_bytes(finalized[4:8], "little") == len(finalized) - 8
    assert int.from_bytes(finalized[data_offset + 4 : data_offset + 8], "little") == (
        len(finalized) - data_offset - 8
    )
    assert finalized[data_offset + 8 :] == streamed[data_offset + 8 :]
    with wave.open(io.BytesIO(finalized), "rb") as wav_file:
        assert wav_file.getnframes() == 32


def test_visual_planning_rejects_pending_review_transcript() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="pending_review",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=[],
            speaker_participant_id="chatgpt",
            text="Pending transcript visuals should not be generated.",
            status="pending_review",
        )
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id

    with pytest.raises(
        ValueError,
        match="transcript must be approved before visual generation",
    ):
        ComfyUiService().plan_visual_assets(
            episode,
            VisualAssetPlanRequest(transcript_version_id=transcript.id),
            visual_profiles=default_visual_profiles(),
            workflows=default_comfyui_workflows(),
        )


@pytest.mark.asyncio
async def test_mock_comfyui_health_reports_visual_capabilities() -> None:
    endpoint = default_comfyui_endpoints()[0]

    checked = await ComfyUiService().check_endpoint_health(endpoint)

    assert checked.health_status == "healthy"
    assert checked.capabilities["prompt"] is True
    assert checked.capabilities["video"] is True


@pytest.mark.asyncio
async def test_remote_comfyui_health_redacts_discovered_device_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/system_stats"
        return httpx.Response(
            200,
            json={
                "devices": [
                    {
                        "name": "cuda:0",
                        "accessToken": "leaked-comfyui-device-token",
                        "nested": {"clientSecret": "leaked-comfyui-device-secret"},
                    }
                ]
            },
        )

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )

    checked = await ComfyUiService(transport=httpx.MockTransport(handler)).check_endpoint_health(
        endpoint
    )

    assert checked.health_status == "healthy"
    assert checked.capabilities["system_stats"] is True
    assert checked.capabilities["devices"][0]["name"] == "cuda:0"
    assert checked.capabilities["devices"][0]["accessToken"] == "[redacted]"
    assert checked.capabilities["devices"][0]["nested"]["clientSecret"] == "[redacted]"
    capabilities_json = json.dumps(checked.capabilities, sort_keys=True)
    assert "leaked-comfyui-device-token" not in capabilities_json
    assert "leaked-comfyui-device-secret" not in capabilities_json


@pytest.mark.asyncio
async def test_b1_native_comfyui_health_uses_object_info_bearer_and_ca(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ca_path = tmp_path / "b1-caddy-root.crt"
    ca_path.write_text("public ca", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer b1-token"
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"KSampler": {"input": {"required": {}}}})
        assert request.url.path == "/prompt"
        assert json.loads(request.content) == {"prompt": {}}
        return httpx.Response(200, json={"KSampler": {"input": {"required": {}}}})

    monkeypatch.setenv("B1_API_KEY", "b1-token")
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "native_comfyui": True,
            "tls_ca_cert_path": str(ca_path),
        },
    )

    checked = await ComfyUiService(transport=httpx.MockTransport(handler)).check_endpoint_health(
        endpoint
    )

    assert [request.url.path for request in requests] == ["/object_info", "/prompt"]
    assert checked.health_status == "healthy"
    assert checked.capabilities["object_info"] is True
    assert checked.capabilities["node_metadata_count"] == 1
    assert checked.capabilities["prompt_admission_ready"] is True
    assert checked.capabilities["prompt_admission_probe"]["status_code"] == 200
    assert checked.capabilities["websocket_path"] == "/ws"
    assert checked.capabilities["required_read_scope"] == "jobs:read"
    assert checked.capabilities["required_write_scope"] == "jobs:write"
    assert checked.capabilities["credential_reference_configured"] is True
    assert checked.capabilities["credential_reference_resolved"] is True
    assert checked.capabilities["tls_ca_cert_file_available"] is True


@pytest.mark.asyncio
async def test_b1_native_comfyui_health_reports_managed_media_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ca_path = tmp_path / "b1-caddy-root.crt"
    ca_path.write_text("public ca", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer b1-token"
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"KSampler": {"input": {"required": {}}}})
        if request.url.path == "/prompt":
            assert json.loads(request.content) == {"prompt": {}}
            return httpx.Response(400, json={"detail": {"code": "prompt_no_outputs"}})
        assert str(request.url) == "https://api.ai.b1.germering/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "image-default", "status": "installed", "enabled": True},
                    {"id": "image-edit", "status": "installed", "enabled": True},
                    {"id": "image-upscale", "status": "installed", "enabled": True},
                    {"id": "video-text", "status": "installed", "enabled": True},
                    {"id": "video-image", "status": "installed", "enabled": True},
                    {"id": "talking-head-lipsync", "status": "installed", "enabled": True},
                    {
                        "id": "studio-seated-character-p40",
                        "status": "installed",
                        "enabled": True,
                    },
                    {"id": "studio-panel-shot", "status": "installed", "enabled": True},
                    {"id": "chat-default", "status": "installed", "enabled": True},
                ],
            },
        )

    monkeypatch.setenv("B1_API_KEY", "b1-token")
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "native_comfyui": True,
            "tls_ca_cert_path": str(ca_path),
            "remote_nodes_api_base": "https://api.ai.b1.germering",
            "credential_reference_error": "stale missing credential",
        },
    )

    checked = await ComfyUiService(transport=httpx.MockTransport(handler)).check_endpoint_health(
        endpoint
    )

    assert [request.url.path for request in requests] == [
        "/object_info",
        "/prompt",
        "/v1/models",
    ]
    assert checked.health_status == "healthy"
    assert checked.capabilities["prompt_admission_ready"] is True
    assert checked.capabilities["managed_media_api"] is True
    assert checked.capabilities["managed_media_catalog_ready"] is True
    assert checked.capabilities["managed_media_model_count"] == 9
    assert checked.capabilities["managed_media_required_presets"] == [
        "image-default",
        "image-edit",
        "image-upscale",
        "video-text",
        "video-image",
        "talking-head-lipsync",
        "studio-seated-character-p40",
        "studio-panel-shot",
    ]
    assert checked.capabilities["managed_media_available_presets"] == [
        "image-default",
        "image-edit",
        "image-upscale",
        "studio-panel-shot",
        "studio-seated-character-p40",
        "talking-head-lipsync",
        "video-image",
        "video-text",
    ]
    assert checked.capabilities["managed_media_available_model_ids"] == [
        "chat-default",
        "image-default",
        "image-edit",
        "image-upscale",
        "studio-panel-shot",
        "studio-seated-character-p40",
        "talking-head-lipsync",
        "video-image",
        "video-text",
    ]
    assert checked.capabilities["managed_media_missing_presets"] == []
    assert "credential_reference_error" not in checked.capabilities


@pytest.mark.asyncio
async def test_b1_native_comfyui_health_reports_missing_managed_media_preset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ca_path = tmp_path / "b1-caddy-root.crt"
    ca_path.write_text("public ca", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer b1-token"
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"KSampler": {"input": {"required": {}}}})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "probe-ok"})
        assert str(request.url) == "https://api.ai.b1.germering/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "image-default", "status": "installed", "enabled": True},
                    {"id": "video-image", "status": "disabled", "enabled": False},
                ]
            },
        )

    monkeypatch.setenv("B1_API_KEY", "b1-token")
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "native_comfyui": True,
            "tls_ca_cert_path": str(ca_path),
            "remote_nodes_api_base": "https://api.ai.b1.germering",
        },
    )

    checked = await ComfyUiService(transport=httpx.MockTransport(handler)).check_endpoint_health(
        endpoint
    )

    assert checked.health_status == "healthy"
    assert checked.capabilities["managed_media_api"] is True
    assert checked.capabilities["managed_media_catalog_ready"] is False
    assert checked.capabilities["managed_media_available_presets"] == ["image-default"]
    assert checked.capabilities["managed_media_available_model_ids"] == ["image-default"]
    assert checked.capabilities["managed_media_missing_presets"] == [
        "image-edit",
        "image-upscale",
        "video-text",
        "video-image",
        "talking-head-lipsync",
        "studio-seated-character-p40",
        "studio-panel-shot",
    ]
    assert checked.capabilities["managed_media_unavailable_presets"] == [
        {
            "id": "video-image",
            "status": "disabled",
            "enabled": False,
            "reason": None,
        }
    ]


@pytest.mark.asyncio
async def test_b1_native_comfyui_health_reports_prompt_admission_503(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ca_path = tmp_path / "b1-caddy-root.crt"
    ca_path.write_text("public ca", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer b1-token"
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"KSampler": {"input": {"required": {}}}})
        assert request.url.path == "/prompt"
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "hardware_resource_policy",
                    "message": "GPU admission blocked by hardware resource policy",
                    "api_key": "leaked-comfy-token",
                }
            },
        )

    monkeypatch.setenv("B1_API_KEY", "b1-token")
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "native_comfyui": True,
            "tls_ca_cert_path": str(ca_path),
        },
    )

    checked = await ComfyUiService(transport=httpx.MockTransport(handler)).check_endpoint_health(
        endpoint
    )

    assert checked.health_status == "unhealthy"
    assert checked.capabilities["object_info"] is True
    assert checked.capabilities["prompt_admission_ready"] is False
    assert checked.capabilities["prompt_admission_probe"]["status_code"] == 503
    assert (
        checked.capabilities["prompt_admission_probe"]["response"]["detail"]["code"]
        == "hardware_resource_policy"
    )
    assert "leaked-comfy-token" not in json.dumps(checked.capabilities, sort_keys=True)


@pytest.mark.asyncio
async def test_b1_native_comfyui_health_reports_missing_credential(monkeypatch) -> None:
    monkeypatch.delenv("B1_API_KEY", raising=False)
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"native_comfyui": True},
    )

    checked = await ComfyUiService().check_endpoint_health(endpoint)

    assert checked.health_status == "unhealthy"
    assert checked.capabilities["credential_reference_configured"] is True
    assert checked.capabilities["credential_reference_resolved"] is False
    assert checked.capabilities["credential_reference_error"] == (
        "credential reference is not available"
    )


@pytest.mark.asyncio
async def test_b1_native_comfyui_health_reports_missing_ca(monkeypatch) -> None:
    monkeypatch.setenv("B1_API_KEY", "b1-token")
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={
            "native_comfyui": True,
            "tls_ca_cert_path": "/missing/b1-caddy-root.crt",
        },
    )

    checked = await ComfyUiService().check_endpoint_health(endpoint)

    assert checked.health_status == "unhealthy"
    assert checked.capabilities["credential_reference_resolved"] is True
    assert checked.capabilities["tls_ca_cert_file_available"] is False


@pytest.mark.asyncio
async def test_comfyui_ca_bootstrap_stores_runtime_state_certificate(
    tmp_path: Path,
) -> None:
    cert_bytes = b"public b1 comfy ca"
    expected_sha256 = hashlib.sha256(cert_bytes).hexdigest()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/.well-known/b1-ai-hub/caddy-root.crt"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=cert_bytes)

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": expected_sha256,
            "tls_ca_cert_path": "/outside/b1-caddy-root.crt",
        },
    )
    service = ComfyUiService(
        Settings(runtime_state_path=str(tmp_path / "runtime-state")),
        transport=httpx.MockTransport(handler),
    )

    bootstrapped = await service.bootstrap_ca_certificate(endpoint)

    stored_path = Path(bootstrapped.capabilities["tls_ca_cert_path"])
    assert stored_path == tmp_path / "runtime-state" / "certificates" / "b1-caddy-root.crt"
    assert stored_path.read_bytes() == cert_bytes
    assert bootstrapped.capabilities["ca_cert_bootstrap"] == {
        "stored": True,
        "sha256_matches": True,
    }
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_comfyui_ca_bootstrap_rejects_sha_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"wrong ca")

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        capabilities={
            "ca_cert_bootstrap_url": (
                "https://ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
            ),
            "ca_cert_sha256": "0" * 64,
        },
    )
    service = ComfyUiService(
        Settings(runtime_state_path=str(tmp_path / "runtime-state")),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="SHA-256"):
        await service.bootstrap_ca_certificate(endpoint)


def test_default_comfyui_workflows_include_patchable_api_templates() -> None:
    workflows = {workflow.id: workflow for workflow in default_comfyui_workflows()}

    assert set(workflows) == {
        "workflow-talking-head-v1",
        "workflow-seated-panel-lipsync-v1",
        "workflow-studio-seated-character-p40-v2",
        "workflow-studio-panel-shot-v1",
        "workflow-reaction-v1",
        "workflow-topic-broll-v1",
        "workflow-studio-wide-v1",
        "workflow-image-edit-v1",
        "workflow-image-upscale-v1",
    }
    assert (
        workflows["workflow-studio-seated-character-p40-v2"].comfyui_endpoint_id
        == "b1-comfyui"
    )
    assert workflows["workflow-studio-panel-shot-v1"].comfyui_endpoint_id == "b1-comfyui"
    for workflow in [
        workflows["workflow-talking-head-v1"],
        workflows["workflow-seated-panel-lipsync-v1"],
        workflows["workflow-studio-seated-character-p40-v2"],
        workflows["workflow-studio-panel-shot-v1"],
        workflows["workflow-reaction-v1"],
        workflows["workflow-topic-broll-v1"],
        workflows["workflow-studio-wide-v1"],
    ]:
        assert workflow.api_workflow
        assert workflow.api_workflow["3"]["inputs"]["ckpt_name"] == (
            "sd-v1-5-pruned-emaonly-b1-video.safetensors"
        )
        assert workflow.api_workflow["11"]["inputs"]["vae"] == ["3", 2]
        assert workflow.api_workflow["13"]["class_type"] == "PrimitiveString"
        assert workflow.api_workflow["14"]["class_type"] == "PrimitiveString"
        assert workflow.api_workflow["18"]["class_type"] == "ConditioningConcat"
        assert workflow.prompt_template["node_input_bindings"]["6.inputs.text"] == (
            "positive_prompt"
        )
        assert workflow.prompt_template["node_input_bindings"]["7.inputs.text"] == (
            "negative_prompt"
        )
        assert workflow.prompt_template["node_input_bindings"]["8.inputs.width"] == "width"
        assert workflow.prompt_template["node_input_bindings"]["8.inputs.height"] == "height"
        assert workflow.prompt_template["node_input_bindings"]["9.inputs.seed"] == "seed"
        assert workflow.prompt_template["node_input_bindings"]["9.inputs.steps"] == "steps"
        assert workflow.prompt_template["node_input_bindings"]["9.inputs.cfg"] == "cfg"
        assert "workflow_capabilities" in workflow.default_parameters

    for workflow_id in {
        "workflow-talking-head-v1",
        "workflow-seated-panel-lipsync-v1",
        "workflow-studio-panel-shot-v1",
        "workflow-reaction-v1",
        "workflow-studio-wide-v1",
    }:
        bindings = workflows[workflow_id].prompt_template["node_input_bindings"]
        assert bindings["8.inputs.batch_size"] == "frame_count"
        assert bindings["10.inputs.fps"] == "fps"
        assert bindings["12.inputs.filename_prefix"] == "filename_prefix"
        assert bindings["15.inputs.camera_motion"] == "camera_motion"
        assert bindings["16.inputs.lighting_preset"] == "lighting_preset"
        assert workflows[workflow_id].api_workflow["8"]["class_type"] == "EmptyLatentImage"
        assert workflows[workflow_id].api_workflow["10"]["class_type"] == "CreateVideo"
        assert workflows[workflow_id].api_workflow["12"]["class_type"] == "SaveVideo"

    assert workflows["workflow-topic-broll-v1"].api_workflow["12"]["class_type"] == (
        "SaveImage"
    )
    assert workflows["workflow-topic-broll-v1"].api_workflow["19"]["class_type"] == (
        "ImageMetadata"
    )
    broll_bindings = workflows["workflow-topic-broll-v1"].prompt_template[
        "node_input_bindings"
    ]
    assert broll_bindings["15.inputs.shot_style"] == "shot_style"
    assert broll_bindings["16.inputs.composition"] == "composition"
    assert broll_bindings["17.inputs.lighting_preset"] == "lighting_preset"
    assert workflows["workflow-topic-broll-v1"].default_parameters[
        "workflow_capabilities"
    ]["source_grounded_prompting"] is True
    assert workflows["workflow-talking-head-v1"].api_workflow["12"]["inputs"]["format"] == "mp4"
    assert workflows["workflow-talking-head-v1"].default_parameters[
        "workflow_capabilities"
    ]["mouth_motion_guidance"] is True
    assert workflows["workflow-studio-wide-v1"].default_parameters["workflow_preset"] == (
        "studio_wide_scene_v1"
    )
    assert workflows["workflow-talking-head-v1"].default_parameters["b1_media_preset"] == (
        "talking-head-lipsync"
    )
    assert workflows["workflow-talking-head-v1"].default_parameters[
        "b1_media_operation"
    ] == "talking-head-lipsync"
    assert workflows["workflow-talking-head-v1"].default_parameters[
        "b1_lipsync_fps"
    ] == 12
    assert workflows["workflow-talking-head-v1"].default_parameters["managed_b1_media_api"] is True
    assert workflows["workflow-studio-wide-v1"].default_parameters["b1_media_preset"] == (
        "video-image"
    )
    assert workflows["workflow-studio-wide-v1"].default_parameters["managed_b1_media_api"] is True
    assert workflows["workflow-topic-broll-v1"].default_parameters["b1_media_preset"] == (
        "image-default"
    )
    assert workflows["workflow-topic-broll-v1"].default_parameters["managed_b1_media_api"] is True
    assert workflows["workflow-topic-broll-v1"].default_parameters["b1_media_width"] == 512
    assert workflows["workflow-topic-broll-v1"].default_parameters["b1_media_height"] == 288
    assert workflows["workflow-image-edit-v1"].enabled is False
    assert workflows["workflow-image-edit-v1"].default_parameters["b1_media_preset"] == (
        "image-edit"
    )
    assert workflows["workflow-image-edit-v1"].default_parameters["managed_b1_media_api"] is True
    assert workflows["workflow-image-upscale-v1"].enabled is False
    assert workflows["workflow-image-upscale-v1"].default_parameters["b1_media_preset"] == (
        "image-upscale"
    )
    assert workflows["workflow-image-upscale-v1"].default_parameters[
        "managed_b1_media_api"
    ] is True


def test_default_talking_head_workflow_patches_motion_and_duration_context() -> None:
    workflow = default_comfyui_workflows()[0]
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        duration_ms=4500,
        width=1920,
        height=1080,
        fps=30,
        status="planned",
        generation_metadata={
            "visual_role": "video_primary",
            "shot_type": "medium_closeup",
            "character_name": "Moderator",
            "prompt_inputs": {
                "style_prompt": "calm professional studio look",
                "negative_prompt": "flat lighting",
                "transcript_text": "We need auditable evidence.",
                "seed": 1001,
            },
        },
    )

    prompt, context, bindings = ComfyUiService()._patched_api_workflow(workflow, asset)

    assert context["frame_count"] == 135
    assert prompt["8"]["inputs"]["batch_size"] == 135
    assert prompt["9"]["inputs"]["seed"] == 1001
    assert prompt["9"]["inputs"]["steps"] == workflow.default_parameters["steps"]
    assert prompt["9"]["inputs"]["cfg"] == workflow.default_parameters["cfg"]
    assert prompt["10"]["inputs"]["fps"] == 30
    assert prompt["15"]["inputs"]["camera_motion"] == "locked_medium_closeup"
    assert prompt["16"]["inputs"]["lighting_preset"] == "soft_key_fill_rim"
    assert "Moderator, calm professional studio look" in prompt["6"]["inputs"]["text"]
    assert "lip-sync drift" in prompt["7"]["inputs"]["text"]
    assert {"path": "8.inputs.batch_size", "value_key": "frame_count"} in bindings
    assert {"path": "10.inputs.fps", "value_key": "fps"} in bindings
    assert {"path": "15.inputs.camera_motion", "value_key": "camera_motion"} in bindings


def test_legacy_primary_talking_head_asset_uses_audio_driven_workflow() -> None:
    service = ComfyUiService()
    workflows = {workflow.id: workflow for workflow in default_comfyui_workflows()}
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        status="completed",
        generation_metadata={
            "visual_role": "video_primary",
            "shot_type": "talking_head",
            "comfyui_workflow_id": "workflow-reaction-v1",
        },
    )

    workflow = service._workflow_for_asset(asset, workflows)

    assert workflow.id == "workflow-talking-head-v1"
    assert workflow.workflow_type == "talking_head"


def test_comfyui_workflow_common_input_names_patch_character_references() -> None:
    workflow = ComfyUiWorkflow(
        id="custom-reference-workflow",
        name="Custom Reference Workflow",
        workflow_type="talking_head",
        comfyui_endpoint_id="mock-comfyui",
        api_workflow={
            "1": {
                "class_type": "CustomReferenceNode",
                "inputs": {
                    "reference_image_uri": "",
                    "portrait_reference_image_uri": "",
                    "full_body_reference_image_uri": "",
                    "wardrobe_reference_image_uri": "",
                    "show_scene_reference_image_uri": "",
                    "show_scene_reference_image_download_url": "",
                },
            }
        },
    )
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        status="planned",
        generation_metadata={
            "visual_role": "video_primary",
            "shot_type": "medium_closeup",
            "character_name": "Claude",
            "prompt_inputs": {
                "reference_image_uri": "object://visuals/claude/portrait.png",
                "portrait_reference_image_uri": "object://visuals/claude/portrait.png",
                "full_body_reference_image_uri": "object://visuals/claude/full-body.png",
                "wardrobe_reference_image_uri": "object://visuals/claude/wardrobe.png",
                "show_scene_reference_image_uri": "object://show/studio.png",
                "show_scene_reference_image_download_url": (
                    "/api/v1/show-media/scene-reference-image/download?uri=object"
                ),
            },
        },
    )

    prompt, context, bindings = ComfyUiService()._patched_api_workflow(workflow, asset)

    inputs = prompt["1"]["inputs"]
    assert inputs["reference_image_uri"] == "object://visuals/claude/portrait.png"
    assert inputs["portrait_reference_image_uri"] == "object://visuals/claude/portrait.png"
    assert inputs["full_body_reference_image_uri"] == "object://visuals/claude/full-body.png"
    assert inputs["wardrobe_reference_image_uri"] == "object://visuals/claude/wardrobe.png"
    assert inputs["show_scene_reference_image_uri"] == "object://show/studio.png"
    assert inputs["show_scene_reference_image_download_url"] == (
        "/api/v1/show-media/scene-reference-image/download?uri=object"
    )
    assert context["show_scene_reference_image_uri"] == "object://show/studio.png"
    assert {
        "path": "1.inputs.full_body_reference_image_uri",
        "value_key": "full_body_reference_image_uri",
    } in bindings


@pytest.mark.asyncio
async def test_visual_asset_plan_creates_primary_and_broll_placeholders() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), Settings()).run(episode)
    approve_canonical_transcript(produced)
    scene_reference_uri = (
        "object://dialecticore/show-media/scene-reference-images/scene.png"
    )
    produced.definition.media.scene_reference_image_uri = scene_reference_uri

    visual_profiles = [
        profile.model_copy(
            update={
                "reference_image_uri": f"object://legacy/{profile.id}/portrait.png",
                "reference_images": [
                    VisualReferenceImage(
                        reference_type="portrait",
                        uri=f"object://typed/{profile.id}/portrait.png",
                    ),
                    VisualReferenceImage(
                        reference_type="full_body",
                        uri=f"object://typed/{profile.id}/full-body.png",
                    ),
                    VisualReferenceImage(
                        reference_type="wardrobe",
                        uri=f"object://typed/{profile.id}/wardrobe.png",
                        filename="jacket.png",
                    ),
                    VisualReferenceImage(
                        reference_type="wardrobe",
                        uri=f"object://typed/{profile.id}/wardrobe-alt.png",
                        filename="scarf.png",
                    ),
                ],
            }
        )
        for profile in default_visual_profiles()
    ]

    planned = ComfyUiService().plan_visual_assets(
        produced,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=visual_profiles,
        workflows=default_comfyui_workflows(),
    )

    assert planned.status == EpisodeStatus.ready
    canonical = next(
        transcript
        for transcript in planned.transcripts
        if transcript.id == planned.canonical_transcript_version_id
    )
    playable_turn_ids = {
        str(turn.id) for turn in canonical.turns if turn.status != "excluded"
    }
    primary_assets = [
        asset
        for asset in planned.assets
        if asset.asset_type == AssetType.video
        and asset.generation_metadata.get("visual_role") == "video_primary"
    ]
    broll_assets = [
        asset
        for asset in planned.assets
        if asset.asset_type == AssetType.broll
        and asset.generation_metadata.get("visual_role") == "broll"
    ]
    reaction_loop_assets = [
        asset
        for asset in planned.assets
        if asset.asset_type == AssetType.reaction_loop
        and asset.source_entity_type == "participant_profile"
    ]
    studio_scene_assets = [
        asset
        for asset in planned.assets
        if asset.asset_type == AssetType.studio_scene
        and asset.source_entity_type == "episode"
    ]
    assert {asset.source_entity_id for asset in primary_assets} == playable_turn_ids
    assert len(broll_assets) >= 1
    assert len(reaction_loop_assets) == 4
    assert len(studio_scene_assets) == 2
    assert {
        asset.generation_metadata.get("visual_role") for asset in studio_scene_assets
    } == {"studio_scene", "studio_group_cutaway"}
    assert all(
        asset.status == "planned"
        for asset in primary_assets + broll_assets + reaction_loop_assets + studio_scene_assets
    )
    first_primary = primary_assets[0]
    assert first_primary.width == 1920
    assert first_primary.height == 1080
    assert first_primary.fps == 30
    shot_plan = first_primary.generation_metadata["shot_plan"]
    assert shot_plan["primary_asset_id"] == str(first_primary.id)
    assert shot_plan["studio_scene_asset_id"] == str(studio_scene_assets[0].id)
    assert any(
        asset.generation_metadata["shot_plan"].get("reusable_reaction_asset_id")
        for asset in primary_assets
    )
    assert first_primary.generation_metadata["fallback_asset_ids"]
    assert first_primary.generation_metadata["comfyui_endpoint_id"] == "mock-comfyui"
    assert first_primary.generation_metadata["comfyui_workflow_id"] == (
        "workflow-talking-head-v1"
    )
    assert first_primary.generation_metadata["prompt_inputs"]["topic"] == (
        planned.central_question
    )
    primary_prompt_inputs = first_primary.generation_metadata["prompt_inputs"]
    primary_visual_profile_id = first_primary.generation_metadata["visual_profile_id"]
    assert primary_prompt_inputs["reference_image_type"] == "portrait"
    assert primary_prompt_inputs["reference_image_uri"].endswith("/portrait.png")
    assert primary_prompt_inputs["reference_image_download_url"] == (
        f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/portrait/download"
    )
    assert set(primary_prompt_inputs["reference_images"]) == {
        "portrait",
        "full_body",
        "wardrobe",
        "wardrobe_references",
    }
    assert primary_prompt_inputs["reference_images"]["portrait"]["download_url"] == (
        f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/portrait/download"
    )
    assert primary_prompt_inputs["reference_image_download_urls"] == {
        "portrait": (
            f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
            "portrait/download"
        ),
        "full_body": (
            f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
            "full_body/download"
        ),
        "wardrobe": (
            f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
            "wardrobe/download"
        ),
        "wardrobe_references": [
            (
                f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
                "wardrobe/download?uri=object%3A%2F%2Ftyped%2F"
                f"{primary_visual_profile_id}%2Fwardrobe.png"
            ),
            (
                f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
                "wardrobe/download?uri=object%3A%2F%2Ftyped%2F"
                f"{primary_visual_profile_id}%2Fwardrobe-alt.png"
            ),
        ],
    }
    assert primary_prompt_inputs["portrait_reference_image_download_url"] == (
        f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/portrait/download"
    )
    assert primary_prompt_inputs["full_body_reference_image_download_url"] == (
        f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/full_body/download"
    )
    assert primary_prompt_inputs["wardrobe_reference_image_download_url"] == (
        f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/wardrobe/download"
    )
    assert [item["filename"] for item in primary_prompt_inputs["wardrobe_reference_images"]] == [
        "jacket.png",
        "scarf.png",
    ]
    assert primary_prompt_inputs["wardrobe_reference_image_download_urls"] == [
        (
            f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
            "wardrobe/download?uri=object%3A%2F%2Ftyped%2F"
            f"{primary_visual_profile_id}%2Fwardrobe.png"
        ),
        (
            f"/api/v1/visual-profiles/{primary_visual_profile_id}/reference-images/"
            "wardrobe/download?uri=object%3A%2F%2Ftyped%2F"
            f"{primary_visual_profile_id}%2Fwardrobe-alt.png"
        ),
    ]
    expected_scene_reference_download_url = (
        "/api/v1/show-media/scene-reference-image/download?uri="
        f"{quote(scene_reference_uri, safe='')}"
    )
    assert primary_prompt_inputs["show_scene_reference_image_uri"] == scene_reference_uri
    assert primary_prompt_inputs["show_scene_reference_image_download_url"] == (
        expected_scene_reference_download_url
    )
    first_broll = broll_assets[0]
    assert first_broll.generation_metadata["prompt_inputs"]["reference_image_type"] == (
        "show_scene"
    )
    assert first_broll.generation_metadata["prompt_inputs"]["reference_image_uri"] == (
        scene_reference_uri
    )
    assert first_broll.generation_metadata["prompt_inputs"][
        "show_scene_reference_image_download_url"
    ] == expected_scene_reference_download_url
    assert first_broll.generation_metadata["prompt_inputs"][
        "reference_image_download_url"
    ] == (
        expected_scene_reference_download_url
    )

    visual_qc = [
        result
        for result in planned.quality_results
        if result.check_type == "visual_asset_plan_completeness"
    ][-1]
    assert visual_qc.status == "pass"
    assert visual_qc.details["required_visual_turn_count"] == len(playable_turn_ids)
    assert visual_qc.details["planned_primary_visual_asset_count"] == len(playable_turn_ids)
    assert visual_qc.details["planned_broll_asset_count"] == len(broll_assets)
    assert visual_qc.details["planned_reaction_loop_asset_count"] == len(
        reaction_loop_assets
    )
    assert visual_qc.details["planned_studio_scene_asset_count"] == 1
    assert visual_qc.details["planned_studio_group_cutaway_asset_count"] == 1
    assert visual_qc.details["shot_planned_turn_count"] == len(playable_turn_ids)
    assert planned.audit_events[-1].event_type == "visual.assets.planned"


@pytest.mark.asyncio
async def test_seated_panel_plan_binds_each_turn_to_a_set_keyframe_and_seat() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), Settings()).run(episode)
    approve_canonical_transcript(produced)
    produced.definition.media.scene_reference_image_uri = "object://dialecticore/studio/reference.png"
    produced.definition.media.directing.studio_layout = "seated_panel"
    produced.definition.media.directing.seating_plan = {"host": 3, "chatgpt": 1}

    planned = ComfyUiService().plan_visual_assets(
        produced,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
    )

    assert planned.status == EpisodeStatus.ready
    keyframes = [
        asset
        for asset in planned.assets
        if asset.generation_metadata.get("visual_role") == "studio_panel_keyframe"
    ]
    seated_characters = [
        asset
        for asset in planned.assets
        if asset.generation_metadata.get("visual_role") == "studio_seated_character"
    ]
    primary_assets = [
        asset
        for asset in planned.assets
        if asset.generation_metadata.get("visual_role") == "video_primary"
    ]
    assert keyframes
    assert len(keyframes) == 1
    assert len(seated_characters) == len(
        {turn.speaker_participant_id for turn in produced.transcripts[-1].turns}
    )
    assert keyframes[0].generation_metadata["prompt_inputs"]["depends_on_asset_ids"] == [
        str(asset.id)
        for asset in sorted(
            seated_characters,
            key=lambda candidate: candidate.generation_metadata["prompt_inputs"]["seat"],
        )
    ]
    assert all(
        asset.generation_metadata["comfyui_workflow_id"]
        == "workflow-studio-seated-character-p40-v2"
        for asset in seated_characters
    )
    assert keyframes[0].generation_metadata["prompt_inputs"]["camera_view"] == "establishing_wide"
    assert keyframes[0].generation_metadata["prompt_inputs"]["speaker_participant_id"] is None
    assert primary_assets
    assert all(
        asset.generation_metadata["comfyui_workflow_id"]
        == "workflow-seated-panel-lipsync-v1"
        for asset in primary_assets
    )
    assert all(
        asset.generation_metadata["prompt_inputs"]["studio_layout"] == "seated_panel"
        and asset.generation_metadata["prompt_inputs"]["scene_keyframe_asset_id"]
        for asset in primary_assets
    )
    assert all(
        asset.generation_metadata["shot_plan"]["speaker_mouth_mode"]
        == "audio_driven_seated_panel"
        for asset in primary_assets
    )
    assert all(
        asset.generation_metadata["prompt_inputs"]["seating_plan"]["host"] == 3
        for asset in keyframes
    )

    first_turn = next(turn for turn in produced.transcripts[-1].turns if turn.status != "excluded")
    original_primary_by_turn = {
        asset.source_entity_id: asset
        for asset in primary_assets
        if asset.status != "replaced"
    }
    editorial_timeline = {
        "segments": [
            {
                "id": f"segment-{turn.id}",
                "source_turn_id": str(turn.id),
                "direction": {
                    "view": (
                        "speaker_close_up"
                        if turn.id == first_turn.id
                        else original_primary_by_turn[str(turn.id)].generation_metadata[
                            "prompt_inputs"
                        ]["camera_view"]
                    ),
                    "action": "slow_push" if turn.id == first_turn.id else "cut",
                },
            }
            for turn in produced.transcripts[-1].turns
            if turn.status != "excluded"
        ]
    }
    replanned_for_edit = ComfyUiService().plan_visual_assets(
        planned,
        VisualAssetPlanRequest(user_id="timeline-editor"),
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
        timeline_override=editorial_timeline,
    )
    edited_primary = next(
        asset
        for asset in reversed(replanned_for_edit.assets)
        if asset.status != "replaced"
        and asset.source_entity_id == str(first_turn.id)
        and asset.generation_metadata.get("visual_role") == "video_primary"
    )
    assert original_primary_by_turn[str(first_turn.id)].status == "replaced"
    assert edited_primary.id != original_primary_by_turn[str(first_turn.id)].id
    assert edited_primary.generation_metadata["prompt_inputs"]["camera_view"] == (
        "speaker_close_up"
    )
    assert edited_primary.generation_metadata["shot_plan"]["camera_action"] == "slow_push"
    assert all(
        original_primary_by_turn[str(turn.id)].status != "replaced"
        for turn in produced.transcripts[-1].turns
        if turn.status != "excluded" and turn.id != first_turn.id
    )

    stale_plate = seated_characters[0]
    stale_keyframe = keyframes[0]
    stale_plate.generation_metadata["comfyui_workflow_id"] = (
        "workflow-studio-seated-character-v1"
    )

    replanned = ComfyUiService().plan_visual_assets(
        planned,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
    )

    assert stale_plate.status == "replaced"
    assert stale_keyframe.status == "replaced"
    active_plates = [
        asset
        for asset in replanned.assets
        if asset.status != "replaced"
        and asset.generation_metadata.get("visual_role") == "studio_seated_character"
    ]
    active_keyframes = [
        asset
        for asset in replanned.assets
        if asset.status != "replaced"
        and asset.generation_metadata.get("visual_role") == "studio_panel_keyframe"
    ]
    assert len(active_plates) == len(seated_characters)
    assert len(active_keyframes) == 1
    assert all(
        asset.generation_metadata["comfyui_workflow_id"]
        == "workflow-studio-seated-character-p40-v2"
        for asset in active_plates
    )
    assert active_keyframes[0].generation_metadata["prompt_inputs"][
        "depends_on_asset_ids"
    ] == [
        str(asset.id)
        for asset in sorted(
            active_plates,
            key=lambda candidate: candidate.generation_metadata["prompt_inputs"]["seat"],
        )
    ]


@pytest.mark.asyncio
async def test_visual_plan_qc_fails_when_reusable_talkshow_assets_are_missing() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), Settings()).run(episode)
    approve_canonical_transcript(produced)
    service = ComfyUiService()
    planned = service.plan_visual_assets(
        produced,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
    )
    canonical = next(
        transcript
        for transcript in planned.transcripts
        if transcript.id == planned.canonical_transcript_version_id
    )
    for asset in planned.assets:
        if asset.asset_type in {AssetType.reaction_loop, AssetType.studio_scene}:
            asset.status = "replaced"

    visual_qc = service._visual_plan_qc(
        planned,
        canonical,
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
    )
    issues = {issue["issue"]: issue for issue in visual_qc.details["issues"]}

    assert visual_qc.status == "fail"
    assert visual_qc.details["failure_count"] >= 2
    assert issues["missing_reusable_studio_scene"]["severity"] == "fail"
    assert any(
        issue["issue"] == "missing_reusable_reaction_loop"
        and issue["severity"] == "fail"
        for issue in visual_qc.details["issues"]
    )
    assert visual_qc.details["missing_reaction_loop_participant_ids"]


@pytest.mark.asyncio
async def test_cited_turns_keep_evidence_in_timeline_review_without_video_overlays(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), settings).run(episode)
    approve_canonical_transcript(produced)
    canonical = next(
        transcript
        for transcript in produced.transcripts
        if transcript.id == produced.canonical_transcript_version_id
    )
    cited_turn = canonical.turns[0]
    cited_turn.claims.append(
        Claim(
            text="AI assistants require explicit review ownership.",
            claim_type="supported",
            evidence_refs=["source-a"],
        )
    )
    produced.assets.append(
        Asset(
            episode_id=produced.id,
            asset_type=AssetType.evidence_pack,
            language="en",
            source_entity_type="episode",
            source_entity_id=str(produced.id),
            checksum="sha256:evidence-pack",
            status="completed",
            generation_metadata={
                "evidence_pack": {
                    "id": "pack-test",
                    "source_index": [
                        {
                            "id": "source-a",
                            "title": "AI Governance Report",
                            "source_type": "government_report",
                            "uri": "https://example.gov/ai-governance",
                            "confidence": 0.92,
                        }
                    ],
                }
            },
        )
    )

    planned = ComfyUiService(settings).plan_visual_assets(
        produced,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=default_visual_profiles(),
        workflows=default_comfyui_workflows(),
    )

    assert not [asset for asset in planned.assets if asset.asset_type == AssetType.citation_card]
    plan_qc = [
        result
        for result in planned.quality_results
        if result.check_type == "visual_asset_plan_completeness"
    ][-1]
    assert plan_qc.status == "pass"
    assert plan_qc.details["required_citation_overlay_turn_count"] == 0
    assert plan_qc.details["planned_citation_card_asset_count"] == 0

    timeline_episode = TimelineService(settings).build_timeline(
        planned,
        TimelineBuildRequest(transcript_version_id=canonical.id, user_id="tester"),
    )
    timeline_asset = next(
        asset for asset in timeline_episode.assets if asset.asset_type == AssetType.timeline
    )
    timeline = timeline_asset.generation_metadata["timeline_json"]
    cited_segment = next(
        segment
        for segment in timeline["segments"]
        if segment["source_turn_id"] == str(cited_turn.id)
    )
    assert cited_segment["citation_overlay_asset_ids"] == []
    assert cited_segment["citations"] == [
        {
            "claim": "AI assistants require explicit review ownership.",
            "evidence_ref": "source-a",
            "source_turn_id": str(cited_turn.id),
        }
    ]
    timeline_qc = [
        result
        for result in timeline_episode.quality_results
        if result.check_type == "timeline_integrity"
    ][-1]
    assert timeline_qc.details["citation_segment_count"] == 1
    assert timeline_qc.details["citation_overlay_linked_segment_count"] == 0


@pytest.mark.asyncio
async def test_visual_asset_plan_fails_qc_when_profile_is_missing() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), Settings()).run(episode)
    approve_canonical_transcript(produced)

    planned = ComfyUiService().plan_visual_assets(
        produced,
        VisualAssetPlanRequest(user_id="tester"),
        visual_profiles=[
            profile
            for profile in default_visual_profiles()
            if profile.id != "visual-skeptic"
        ],
        workflows=default_comfyui_workflows(),
    )

    visual_qc = [
        result
        for result in planned.quality_results
        if result.check_type == "visual_asset_plan_completeness"
    ][-1]
    issues = visual_qc.details["issues"]
    assert visual_qc.status == "fail"
    assert any(issue["issue"] == "missing_visual_profile" for issue in issues)
    assert visual_qc.details["planned_primary_visual_asset_count"] < (
        visual_qc.details["required_visual_turn_count"]
    )


@pytest.mark.asyncio
async def test_mock_visual_generation_stores_render_ready_mock_visuals(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    service = ComfyUiService(settings)

    generated = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )

    visual_assets = [
        asset
        for asset in generated.assets
        if asset.asset_type
        in {
            AssetType.video,
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
        }
    ]
    assert visual_assets
    directed_assets = [
        asset
        for asset in visual_assets
        if asset.generation_metadata.get("visual_role")
        in {"studio_scene", "studio_group_cutaway", "reaction_loop"}
    ]
    assert directed_assets
    assert all(
        asset.generation_metadata.get("fallback_visual") is not True
        for asset in directed_assets
    )
    assert all(
        asset.status in {"planned", "completed", "submitted"}
        for asset in directed_assets
    )
    assert all(
        asset.status == "completed"
        for asset in visual_assets
        if asset.generation_metadata.get("visual_role")
        not in {"studio_scene", "studio_group_cutaway", "reaction_loop"}
    )
    completed_visual_assets = [
        asset for asset in visual_assets if asset.status == "completed"
    ]
    assert all(
        asset.storage_uri and asset.storage_uri.startswith("object://")
        for asset in completed_visual_assets
    )
    assert all(
        asset.checksum and asset.checksum.startswith("sha256:")
        for asset in completed_visual_assets
    )
    assert all(asset.mime_type == "image/svg+xml" for asset in completed_visual_assets)
    assert all(
        asset.generation_metadata["deterministic_mock_visual"] is True
        for asset in completed_visual_assets
    )
    assert all(
        asset.generation_metadata["render_ready"] is True
        for asset in completed_visual_assets
    )
    assert all(
        asset.generation_metadata["requires_static_image_duration"] is True
        for asset in completed_visual_assets
    )
    assert all(
        Path(asset.generation_metadata["object_storage_path"]).exists()
        for asset in completed_visual_assets
    )
    visual_qc = [
        result
        for result in generated.quality_results
        if result.check_type == "visual_generation_completeness"
    ][-1]
    assert visual_qc.status == "pass"
    assert visual_qc.details["completed_visual_asset_count"] == len(completed_visual_assets)
    assert visual_qc.details["stored_visual_asset_count"] == len(completed_visual_assets)
    assert visual_qc.details["render_suitable_visual_asset_count"] == len(completed_visual_assets)
    assert generated.audit_events[-2].event_type == "visual.assets.generated"


@pytest.mark.asyncio
async def test_wall_screen_broll_is_a_source_bound_deterministic_card(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    service = ComfyUiService(settings)
    target = next(asset for asset in planned.assets if asset.asset_type == AssetType.broll)
    target.generation_metadata = {
        **target.generation_metadata,
        "visual_role": "wall_screen_broll",
        "prompt_inputs": {
            **target.generation_metadata.get("prompt_inputs", {}),
            "transcript_text": "Abwärme muss in ein verbindliches kommunales Wärmenetz fließen.",
        },
    }

    generated = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target.id], regenerate=True, user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )

    completed = next(asset for asset in generated.assets if asset.id == target.id)
    assert completed.status == "completed"
    assert completed.mime_type == "image/png"
    assert completed.generation_metadata["adapter"] == "dialecticore-wall-screen-card-png/v1"
    assert completed.generation_metadata["deterministic_wall_screen_card"] is True
    assert completed.generation_metadata["source_bound_visual"] is True
    payload = Path(completed.generation_metadata["object_storage_path"]).read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert service._png_dimensions(payload[:24]) == (target.width, target.height)
    assert service._wall_screen_card_title(
        target.generation_metadata["prompt_inputs"]["transcript_text"]
    ) == "Abwärme nutzen"


def test_seated_panel_slow_push_keeps_desk_safe_medium_coverage() -> None:
    view, action, paired = ComfyUiService._seated_panel_directing_decision(
        turn_index=3,
        speaker_participant_id="mistral",
        seating={
            "chatgpt": 1,
            "claude": 2,
            "deepseek": 3,
            "gemini": 4,
            "mistral": 5,
            "grok": 6,
        },
        turn_type=None,
    )

    assert view == "speaker_medium"
    assert action == "slow_push"
    assert paired == []


@pytest.mark.asyncio
async def test_remote_visual_generation_submits_and_syncs_result(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.broll)
    calls: list[tuple[str, str]] = []
    captured_prompt: dict = {}
    flat_png = png_rgba(1, 1, [(0, 0, 0, 255)])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/prompt":
            payload = json_from_request(request)
            captured_prompt.update(payload["prompt"])
            assert payload["extra_data"]["asset_id"] == str(target_asset.id)
            assert payload["extra_data"]["visual_role"] == "broll"
            return httpx.Response(200, json={"status": "queued", "prompt_id": "visual-job-1"})
        if request.method == "GET" and request.url.path == "/history/visual-job-1":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "prompt_id": "visual-job-1",
                    "image_base64": base64.b64encode(flat_png).decode(),
                    "mime_type": "image/png",
                    "render_ready": True,
                    "access_token": "leaked-visual-token",
                    "accessToken": "leaked-visual-camel-token",
                    "nested": {
                        "client_secret": "leaked-visual-secret",
                        "clientSecret": "leaked-visual-camel-secret",
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    submitted = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )
    submitted_asset = next(asset for asset in submitted.assets if asset.id == target_asset.id)
    assert submitted_asset.status == "submitted"
    assert submitted_asset.generation_metadata["remote_job_id"] == "visual-job-1"
    assert "editorial B-roll for How will AI change professional software development?" in (
        captured_prompt["6"]["inputs"]["text"]
    )
    assert captured_prompt["7"]["inputs"]["text"] == (
        "logos, unreadable text, misleading chart labels, fake UI, sensational imagery"
    )
    assert captured_prompt["8"]["inputs"]["width"] == 1920
    assert captured_prompt["8"]["inputs"]["height"] == 1080
    assert captured_prompt["9"]["inputs"]["seed"] is not None
    assert captured_prompt["9"]["inputs"]["steps"] == 30
    assert captured_prompt["9"]["inputs"]["cfg"] == 7.0
    assert captured_prompt["15"]["inputs"]["shot_style"] == "editorial_insert"
    assert captured_prompt["17"]["inputs"]["lighting_preset"] == "documentary_neutral"
    assert submitted_asset.generation_metadata["workflow_patch_bindings"]

    synced = await service.sync_visual_results(
        submitted,
        VisualResultSyncRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )
    synced_asset = next(asset for asset in synced.assets if asset.id == target_asset.id)
    assert synced_asset.status == "completed"
    assert synced_asset.storage_uri and synced_asset.storage_uri.startswith("object://")
    assert synced_asset.mime_type == "image/png"
    assert synced_asset.width == 1
    assert synced_asset.height == 1
    assert synced_asset.generation_metadata["media_probe"]["probe_tool"] == "image_header"
    assert synced_asset.generation_metadata["media_probe"]["render_ready"] is True
    assert synced_asset.generation_metadata["media_probe"]["pixel_analysis"][
        "analysis_tool"
    ] == "png_pixels"
    assert synced_asset.generation_metadata["provider_response"]["access_token"] == (
        "[redacted]"
    )
    assert synced_asset.generation_metadata["provider_response"]["accessToken"] == (
        "[redacted]"
    )
    assert synced_asset.generation_metadata["provider_response"]["nested"][
        "client_secret"
    ] == "[redacted]"
    assert synced_asset.generation_metadata["provider_response"]["nested"][
        "clientSecret"
    ] == "[redacted]"
    metadata_json = json.dumps(synced_asset.generation_metadata, sort_keys=True)
    assert "leaked-visual-token" not in metadata_json
    assert "leaked-visual-camel-token" not in metadata_json
    assert "leaked-visual-secret" not in metadata_json
    assert "leaked-visual-camel-secret" not in metadata_json
    assert Path(synced_asset.generation_metadata["object_storage_path"]).exists()
    assert ("POST", "/prompt") in calls
    assert ("GET", "/history/visual-job-1") in calls
    visual_qc = [
        result
        for result in synced.quality_results
        if result.check_type == "visual_generation_completeness"
    ][-1]
    assert visual_qc.status == "pass"
    assert visual_qc.details["completed_visual_asset_count"] == 1
    assert visual_qc.details["probed_visual_asset_count"] == 1
    assert visual_qc.details["render_ready_visual_asset_count"] == 1
    assert synced.audit_events[-1].event_type == "visual.jobs.synced"

    checked = service.run_visual_quality(
        synced,
        VisualQualityRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )
    media_qc = [
        result
        for result in checked.quality_results
        if result.check_type == "visual_media_integrity"
    ][-1]
    assert media_qc.status == "warning"
    assert media_qc.details["checked_visual_asset_count"] == 1
    assert media_qc.details["completed_visual_asset_count"] == 1
    assert media_qc.details["render_suitable_visual_asset_count"] == 1
    assert media_qc.details["dimension_checked_visual_asset_count"] == 1
    assert media_qc.details["dimension_mismatch_visual_asset_count"] == 1
    assert media_qc.details["pixel_analyzed_visual_asset_count"] == 1
    assert media_qc.details["pixel_warning_visual_asset_count"] == 1
    assert media_qc.details["failure_count"] == 0
    issues = {issue["issue"] for issue in media_qc.details["issues"]}
    assert "visual_dimension_mismatch" in issues
    assert "visual_pixel_analysis_warning" in issues
    assert checked.audit_events[-1].event_type == "visual.qc.completed"


@pytest.mark.asyncio
async def test_remote_visual_generation_uses_renderable_fallback_on_submit_failure(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/prompt"
        return httpx.Response(503, json={"status": "failed", "error": "busy"})

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    generated = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )

    fallback_asset = next(asset for asset in generated.assets if asset.id == target_asset.id)
    assert fallback_asset.status == "completed"
    assert fallback_asset.mime_type == "image/svg+xml"
    assert fallback_asset.storage_uri and fallback_asset.storage_uri.endswith(".svg")
    assert fallback_asset.generation_metadata["fallback_visual"] is True
    assert fallback_asset.generation_metadata["fallback_kind"] == "citation_card"
    assert fallback_asset.generation_metadata["fallback_source_status"] == "submission_error"
    assert fallback_asset.generation_metadata["media_probe"]["probe_tool"] == "svg_header"
    assert fallback_asset.generation_metadata["media_probe"]["render_ready"] is True
    assert fallback_asset.generation_metadata["render_ready"] is True
    assert fallback_asset.width == 1920
    assert fallback_asset.height == 1080
    assert Path(fallback_asset.generation_metadata["object_storage_path"]).exists()

    visual_qc = [
        result
        for result in generated.quality_results
        if result.check_type == "visual_generation_completeness"
    ][-1]
    assert visual_qc.status == "warning"
    assert visual_qc.details["completed_visual_asset_count"] == 1
    assert visual_qc.details["failed_visual_asset_count"] == 0
    assert visual_qc.details["fallback_visual_asset_count"] == 1
    assert visual_qc.details["render_suitable_visual_asset_count"] == 1
    assert visual_qc.details["warning_count"] == 1
    assert visual_qc.details["issues"][0]["issue"] == "visual_fallback_used"
    assert generated.audit_events[-2].details["fallback_count"] == 1


@pytest.mark.asyncio
async def test_visual_generation_can_create_local_fallback_primary_visuals(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    service = ComfyUiService(settings)

    generated = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(
            asset_ids=[target_asset.id],
            user_id="tester",
            local_fallback_only=True,
        ),
        endpoints=[],
        workflows=default_comfyui_workflows(),
    )

    fallback_asset = next(asset for asset in generated.assets if asset.id == target_asset.id)
    assert fallback_asset.status == "completed"
    assert fallback_asset.mime_type == "image/svg+xml"
    assert fallback_asset.generation_metadata["fallback_visual"] is True
    assert fallback_asset.generation_metadata["fallback_source_status"] == (
        "local_fallback_only"
    )
    assert fallback_asset.generation_metadata["render_ready"] is True
    assert fallback_asset.width == 1920
    assert fallback_asset.height == 1080
    assert fallback_asset.storage_uri and fallback_asset.storage_uri.endswith(".svg")

    visual_qc = [
        result
        for result in generated.quality_results
        if result.check_type == "visual_generation_completeness"
    ][-1]
    assert visual_qc.status == "warning"
    assert visual_qc.details["completed_visual_asset_count"] == 1
    assert visual_qc.details["fallback_visual_asset_count"] == 1
    assert visual_qc.details["render_suitable_visual_asset_count"] == 1
    assert visual_qc.details["failure_count"] == 0
    assert generated.audit_events[-2].details["fallback_count"] == 1


@pytest.mark.asyncio
async def test_remote_visual_sync_uses_fallback_when_provider_reports_failed(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.broll)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/prompt":
            return httpx.Response(200, json={"status": "queued", "prompt_id": "visual-job-3"})
        if request.method == "GET" and request.url.path == "/history/visual-job-3":
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "prompt_id": "visual-job-3",
                    "error": "model output rejected",
                    "authorization": "Bearer leaked-fallback-token",
                    "nested": {"api_key": "leaked-fallback-api-key"},
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    submitted = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )
    synced = await service.sync_visual_results(
        submitted,
        VisualResultSyncRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )

    fallback_asset = next(asset for asset in synced.assets if asset.id == target_asset.id)
    assert fallback_asset.status == "completed"
    assert fallback_asset.mime_type == "image/svg+xml"
    assert fallback_asset.generation_metadata["fallback_visual"] is True
    assert fallback_asset.generation_metadata["fallback_kind"] == "fallback_still"
    assert fallback_asset.generation_metadata["fallback_source_status"] == "failed"
    assert fallback_asset.generation_metadata["fallback_provider_metadata"][
        "provider_response"
    ]["error"] == "model output rejected"
    assert fallback_asset.generation_metadata["fallback_provider_metadata"][
        "provider_response"
    ]["authorization"] == "[redacted]"
    assert fallback_asset.generation_metadata["fallback_provider_metadata"][
        "provider_response"
    ]["nested"]["api_key"] == "[redacted]"
    metadata_json = json.dumps(fallback_asset.generation_metadata, sort_keys=True)
    assert "leaked-fallback-token" not in metadata_json
    assert "leaked-fallback-api-key" not in metadata_json
    assert fallback_asset.generation_metadata["sync_attempt_count"] == 1

    visual_qc = [
        result
        for result in synced.quality_results
        if result.check_type == "visual_generation_completeness"
    ][-1]
    assert visual_qc.status == "warning"
    assert visual_qc.details["fallback_visual_asset_count"] == 1
    assert visual_qc.details["render_suitable_visual_asset_count"] == 1
    assert synced.audit_events[-1].details["fallback_count"] == 1


@pytest.mark.asyncio
async def test_remote_visual_sync_preserves_native_directed_provider_failure(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(
        asset
        for asset in planned.assets
        if asset.generation_metadata.get("visual_role") == "video_primary"
    )
    target_asset.status = "submitted"
    target_asset.generation_metadata = {
        **target_asset.generation_metadata,
        "remote_job_id": "native-video-failed",
        "native_camera_coverage_rejected": True,
        "prompt_inputs": {
            **target_asset.generation_metadata.get("prompt_inputs", {}),
            "studio_layout": "seated_panel",
            "speaker_participant_id": "host",
            "paired_participant_ids": [],
            "camera_view": "speaker_medium",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/history/native-video-failed"
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "prompt_id": "native-video-failed",
                "failure_category": "gpu_runner_error",
                "failure_message": (
                    "lan-p40-media runtime recover hook returned HTTP 503: "
                    '{"memory_used_mib":744,"idle_threshold_mib":512}'
                ),
            },
        )

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    synced = await service.sync_visual_results(
        planned,
        VisualResultSyncRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )

    failed_asset = next(asset for asset in synced.assets if asset.id == target_asset.id)
    assert failed_asset.status == "failed"
    assert failed_asset.storage_uri is None
    assert failed_asset.generation_metadata["provider_failure_category"] == (
        "gpu_runner_error"
    )
    assert "idle_threshold_mib" in failed_asset.generation_metadata[
        "provider_failure_message"
    ]
    assert failed_asset.generation_metadata["failure"] == (
        failed_asset.generation_metadata["provider_failure_message"]
    )
    assert failed_asset.generation_metadata.get("fallback_visual") is not True
    assert failed_asset.generation_metadata.get("native_camera_coverage_rejected") is not True
    assert synced.audit_events[-1].details["fallback_count"] == 0
    assert synced.audit_events[-1].details["failed_count"] == 1


@pytest.mark.asyncio
async def test_visual_media_qc_warns_when_primary_lipsync_is_not_ready(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    service = ComfyUiService(settings)
    generated = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )
    primary_asset = next(
        asset
        for asset in generated.assets
        if asset.asset_type == AssetType.video
        and asset.generation_metadata.get("visual_role") == "video_primary"
    )
    primary_asset.duration_ms = 4000
    primary_asset.generation_metadata["provider_lip_sync_offset_ms"] = 725
    generated.assets.append(
        Asset(
            episode_id=generated.id,
            asset_type=AssetType.audio,
            language=primary_asset.language,
            source_entity_type="transcript_turn",
            source_entity_id=primary_asset.source_entity_id,
            storage_uri="object://dialecticore/audio/not-ready.wav",
            mime_type="audio/wav",
            duration_ms=1000,
            checksum="sha256:audio",
            status="completed",
            generation_metadata={
                "phoneme_timing": {
                    "ready_for_lipsync": False,
                    "phoneme_count": 0,
                    "source": "missing",
                }
            },
        )
    )

    checked = service.run_visual_quality(
        generated,
        VisualQualityRequest(asset_ids=[primary_asset.id], user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )

    media_qc = [
        result
        for result in checked.quality_results
        if result.check_type == "visual_media_integrity"
    ][-1]
    issues = {issue["issue"] for issue in media_qc.details["issues"]}
    assert media_qc.status == "warning"
    assert media_qc.details["checked_visual_asset_count"] == 1
    assert media_qc.details["lip_sync_ready_visual_asset_count"] == 0
    assert media_qc.details["lip_sync_missing_visual_asset_count"] == 1
    assert media_qc.details["lip_sync_measured_visual_asset_count"] == 1
    assert media_qc.details["max_lip_sync_offset_ms"] == 725
    assert media_qc.details["average_lip_sync_offset_ms"] == 725
    assert media_qc.details["duration_checked_visual_asset_count"] == 1
    assert media_qc.details["duration_mismatch_visual_asset_count"] == 1
    assert media_qc.details["style_metadata_missing_visual_asset_count"] == 0
    mismatch_issue = next(
        issue
        for issue in media_qc.details["issues"]
        if issue["issue"] == "visual_audio_duration_mismatch"
    )
    assert mismatch_issue["lip_sync_offset_ms"] == 725
    assert "visual_lipsync_not_ready" in issues
    assert "visual_audio_duration_mismatch" in issues


def test_visual_media_qc_flags_invalid_video_probe_requirements() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
        turns=[
            TranscriptTurn(
                source_discussion_turn_ids=[],
                speaker_participant_id="host",
                text="Video probe requirements need evidence.",
            )
        ],
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    studio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        language="en",
        source_entity_type="episode",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/visuals/studio.mp4",
        mime_type="video/mp4",
        duration_ms=1000,
        width=1920,
        height=1080,
        checksum="sha256:studio",
        status="completed",
        generation_metadata={
            "visual_role": "studio_scene",
            "render_ready": False,
            "media_probe": {
                "probe_tool": "ffprobe",
                "probe_warnings": [
                    "video probe missing fps",
                    "video probe missing frame count",
                ],
                "width": 1920,
                "height": 1080,
                "duration_ms": 1000,
                "fps": None,
                "frame_count": None,
                "render_ready": False,
                "video_analysis": {
                    "analysis_tool": "ffprobe_video_integrity",
                    "codec_name": "h264",
                    "pixel_format": "yuv420p",
                    "critical_warnings": [
                        "video probe missing fps",
                        "video probe missing frame count",
                    ],
                    "frame_count_source": None,
                    "estimated_frame_count": None,
                },
            },
        },
    )
    episode.assets.append(studio_asset)

    checked = ComfyUiService().run_visual_quality(
        episode,
        VisualQualityRequest(asset_ids=[studio_asset.id], user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )

    media_qc = [
        result
        for result in checked.quality_results
        if result.check_type == "visual_media_integrity"
    ][-1]
    issues = {issue["issue"] for issue in media_qc.details["issues"]}
    assert media_qc.status == "warning"
    assert media_qc.details["video_probe_checked_visual_asset_count"] == 1
    assert media_qc.details["video_probe_warning_visual_asset_count"] == 1
    assert media_qc.details["video_probe_invalid_visual_asset_count"] == 1
    assert media_qc.details["video_probe_missing_frame_count_visual_asset_count"] == 1
    assert media_qc.details["video_probe_estimated_frame_count_visual_asset_count"] == 0
    assert "visual_video_probe_missing_fps" in issues
    assert "visual_video_probe_missing_frame_count" in issues
    assert "visual_asset_not_render_suitable" in issues


@pytest.mark.asyncio
async def test_visual_media_qc_scores_character_identity_and_style_consistency(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    service = ComfyUiService(settings)
    generated = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )
    primary_asset = next(
        asset
        for asset in generated.assets
        if asset.asset_type == AssetType.video
        and asset.generation_metadata.get("visual_role") == "video_primary"
    )
    primary_asset.generation_metadata["character_name"] = "Wrong Character"
    primary_asset.generation_metadata["prompt_inputs"]["style_prompt"] = (
        "different visual style"
    )

    checked = service.run_visual_quality(
        generated,
        VisualQualityRequest(asset_ids=[primary_asset.id], user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )

    media_qc = [
        result
        for result in checked.quality_results
        if result.check_type == "visual_media_integrity"
    ][-1]
    issues = {issue["issue"]: issue for issue in media_qc.details["issues"]}
    assert media_qc.status == "warning"
    assert media_qc.details["identity_consistency_checked_visual_asset_count"] == 1
    assert media_qc.details["identity_consistency_warning_visual_asset_count"] == 1
    assert media_qc.details["style_consistency_checked_visual_asset_count"] == 1
    assert media_qc.details["style_consistency_warning_visual_asset_count"] == 1
    assert issues["visual_character_identity_mismatch"]["identity_score"] == 0.0
    assert issues["visual_character_identity_mismatch"][
        "expected_character_name"
    ] == "Moderator"
    assert issues["visual_style_prompt_mismatch"]["style_score"] == 0.0
    assert issues["visual_style_prompt_mismatch"]["expected_style_prompt"] == (
        "calm professional moderator in a modern studio"
    )


@pytest.mark.asyncio
async def test_remote_visual_generation_patches_workflow_node_inputs(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    captured_prompt: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/prompt"
        payload = json_from_request(request)
        captured_prompt.update(payload["prompt"])
        return httpx.Response(200, json={"status": "queued", "prompt_id": "visual-job-patch"})

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflow = default_comfyui_workflows()[0].model_copy(
        update={
            "comfyui_endpoint_id": "comfyui-remote",
            "api_workflow": {
                "6": {"inputs": {"text": ""}},
                "7": {"inputs": {"text": ""}},
                "8": {"inputs": {"width": 0, "height": 0, "seed": 0}},
            },
            "prompt_template": {
                "positive": "{character_name} discusses {topic}. {style_prompt}",
                "negative": "{negative_prompt}, unreadable text",
                "node_input_bindings": {
                    "6.inputs.text": "positive_prompt",
                    "7.inputs.text": "negative_prompt",
                    "8.inputs.width": "width",
                    "8.inputs.height": "height",
                    "8.inputs.seed": "seed",
                },
            },
        }
    )
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    submitted = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=[workflow, *default_comfyui_workflows()[1:]],
    )

    assert "Moderator discusses How will AI change professional software development?" in (
        captured_prompt["6"]["inputs"]["text"]
    )
    assert captured_prompt["7"]["inputs"]["text"].endswith("unreadable text")
    assert captured_prompt["8"]["inputs"] == {"width": 1920, "height": 1080, "seed": 1001}
    submitted_asset = next(asset for asset in submitted.assets if asset.id == target_asset.id)
    bindings = submitted_asset.generation_metadata["workflow_patch_bindings"]
    assert {"path": "6.inputs.text", "value_key": "positive_prompt"} in bindings
    assert submitted_asset.generation_metadata["resolved_prompt_inputs"]["width"] == 1920


@pytest.mark.asyncio
async def test_b1_managed_media_generation_submits_media_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("B1_API_KEY", "b1-token")
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    audio_asset = prepare_b1_lipsync_inputs(settings, planned, target_asset)
    captured_payload: dict = {}
    upload_fields: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "api.ai.b1.germering"
        assert request.headers["authorization"] == "Bearer b1-token"
        if request.url.path == "/v1/media/uploads":
            field = request.headers["x-b1-field"]
            upload_fields.append(field)
            if field == "portrait":
                assert request.headers["content-type"] == "image/png"
                return httpx.Response(
                    200,
                    json={"reference": {"id": "upload-portrait", "field": "portrait"}},
                )
            assert field == "audio"
            assert request.headers["content-type"] == "audio/wav"
            return httpx.Response(200, json={"reference": "upload-audio"})
        assert request.url.path == "/v1/media/jobs"
        assert request.headers["idempotency-key"] == (
            f"dialecticore-visual-{target_asset.id}-attempt-1"
        )
        captured_payload.update(json_from_request(request))
        return httpx.Response(
            202,
            json={
                "object": "b1.async_job",
                "b1_job_id": "job-managed-1",
                "b1_status": "queued",
                "links": {
                    "self": "/v1/media/jobs/job-managed-1",
                    "artifacts": "/v1/media/jobs/job-managed-1/artifacts",
                },
            },
        )

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"remote_nodes_api_base": "https://api.ai.b1.germering"},
    )
    workflow = default_comfyui_workflows()[0].model_copy(
        update={"comfyui_endpoint_id": "b1-comfyui"}
    )
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    submitted = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=[workflow, *default_comfyui_workflows()[1:]],
    )

    assert captured_payload["modality"] == "video"
    assert upload_fields == ["portrait", "audio"]
    assert captured_payload["operation"] == "talking-head-lipsync"
    assert captured_payload["model"] == "talking-head-lipsync"
    assert captured_payload["runtime_policy"] == "comfyui"
    assert captured_payload["input"]["portrait_artifact_id"] == "upload-portrait"
    assert captured_payload["input"]["audio_artifact_id"] == "upload-audio"
    assert captured_payload["input"]["audio_sha256"] == hashlib.sha256(
        Path(create_object_store(settings).path_for_uri(audio_asset.storage_uri)).read_bytes()
    ).hexdigest()
    assert captured_payload["input"]["duration_ms"] == 1000
    assert captured_payload["input"]["width"] == 512
    assert captured_payload["input"]["height"] == 512
    assert captured_payload["input"]["fps"] == 12
    assert captured_payload["input"]["performance_plan"]["schema_version"] == (
        "dialecticore.character_performance.v1"
    )
    assert captured_payload["input"]["performance_plan"]["on_camera_energy"] == "measured"
    submitted_asset = next(asset for asset in submitted.assets if asset.id == target_asset.id)
    assert submitted_asset.status == "submitted"
    assert submitted_asset.generation_metadata["adapter"] == "b1_managed_media"
    assert submitted_asset.generation_metadata["remote_job_id"] == "job-managed-1"
    assert submitted_asset.generation_metadata["lip_sync_mode"] == "audio_driven"
    assert submitted_asset.generation_metadata["portrait_repacked_for_b1"] is False
    assert submitted_asset.generation_metadata["b1_portrait_content_type"] == "image/png"
    assert (
        submitted_asset.generation_metadata["managed_media_api_base"]
        == "https://api.ai.b1.germering"
    )


def test_b1_image_transport_preserves_alpha_png_bytes() -> None:
    source = png_rgba(
        2,
        2,
        [(40, 90, 180, 128), (40, 90, 180, 0), (40, 90, 180, 255), (1, 2, 3, 4)],
    )

    payload, content_type, repacked = ComfyUiService(Settings())._prepare_b1_image_for_upload(
        source,
        "image/png",
    )

    assert payload == source
    assert hashlib.sha256(payload).digest() == hashlib.sha256(source).digest()
    assert content_type == "image/png"
    assert repacked is False


@pytest.mark.asyncio
async def test_seated_lipsync_reuses_b1_panel_reference_and_selects_speaker_face_region() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    scene = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id=f"{episode.id}:panel:speaker_medium:host:solo",
        storage_uri="object://dialecticore/panel/keyframe.png",
        mime_type="image/png",
        status="completed",
        width=1280,
        height=720,
        generation_metadata={
            "studio_panel": {
                "scene_artifact_id": "upload-b1-panel-scene",
                "seat_map": [
                    {
                        "participant_id": "host",
                        "seat": 2,
                        "face_region": {"x": 0.41, "y": 0.18, "width": 0.16, "height": 0.23},
                    },
                    {
                        "participant_id": "chatgpt",
                        "seat": 1,
                        "face_region": {"x": 0.12, "y": 0.22, "width": 0.14, "height": 0.21},
                    },
                ],
            }
        },
    )
    primary = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-host",
        generation_metadata={
            "prompt_inputs": {
                "scene_keyframe_asset_id": str(scene.id),
                "speaker_participant_id": "host",
                "camera_view": "speaker_close_up",
                "camera_action": "slow_push",
                "seating_plan": {"chatgpt": 1, "host": 2},
            }
        },
    )
    episode.assets.append(scene)

    async def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(
            "B1 should not receive an upload when its scene reference is reusable: "
            f"{request.url}"
        )

    service = ComfyUiService(transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload, metadata = await service._b1_seated_panel_lipsync_inputs(
            client=client,
            api_base="https://api.ai.b1.germering",
            episode=episode,
            asset=primary,
        )

    assert payload["scene_artifact_id"] == "upload-b1-panel-scene"
    assert payload["speaker_participant_id"] == "host"
    assert payload["camera_view"] == "speaker_close"
    assert payload["framed_participant_ids"] == ["host"]
    assert payload["camera"] == {
        "view": "speaker_close",
        "action": "cut",
        "composition": "native_scene_camera",
    }
    assert payload["face_regions"] == [
        {
            "participant_id": "host",
            "seat": 2,
            "face_region": {"x": 0.41, "y": 0.18, "width": 0.16, "height": 0.23},
        }
    ]
    assert metadata["seated_panel"]["scene_reference_source"] == "b1_studio_panel_result"
    assert metadata["seated_panel"]["scene_sha256"] is None
    assert metadata["seated_panel"]["camera_view"] == "speaker_close_up"
    assert metadata["seated_panel"]["b1_camera_view"] == "speaker_close"
    assert metadata["seated_panel"]["camera_action"] == "cut"
    assert metadata["seated_panel"]["requested_camera_action"] == "slow_push"
    assert metadata["seated_panel"]["framed_participant_ids"] == ["host"]
    assert metadata["seated_panel"]["camera_composition"] == "native_scene_camera"


def test_seated_panel_native_camera_coverage_rejects_insufficient_provider_evidence() -> None:
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-grok",
        generation_metadata={
            "visual_role": "video_primary",
            "prompt_inputs": {
                "studio_layout": "seated_panel",
                "speaker_participant_id": "grok",
                "paired_participant_ids": [],
                "camera_view": "speaker_medium",
            },
        },
    )
    result = VisualResult(
        status="completed",
        storage_uri="object://dialecticore/visual/grok.mp4",
        mime_type="video/mp4",
        duration_ms=1000,
        width=1024,
        height=576,
        fps=12,
        checksum="sha256:video",
        metadata={
            "studio_panel": {
                "actual_camera_view": "speaker_medium",
                "camera_composition": "native_scene_camera",
                "speaker_face_region_px": {"x": 1, "y": 2, "width": 120, "height": 139},
                "framed_participant_ids": ["grok"],
            }
        },
    )

    service = ComfyUiService()
    service._apply_visual_result(asset, result, submitted_by="tester")

    assert asset.status == "failed"
    assert asset.generation_metadata["provider_failure_category"] == (
        "native_camera_coverage_rejected"
    )
    assert "requires 140px" in asset.generation_metadata["failure"]


def test_seated_panel_native_camera_coverage_accepts_matching_provider_evidence() -> None:
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-grok-mistral",
        generation_metadata={
            "visual_role": "video_primary",
            "prompt_inputs": {
                "studio_layout": "seated_panel",
                "speaker_participant_id": "grok",
                "paired_participant_ids": ["mistral"],
                "camera_view": "panel_two_shot",
            },
        },
    )
    result = VisualResult(
        status="completed",
        storage_uri="object://dialecticore/visual/grok-mistral.mp4",
        mime_type="video/mp4",
        duration_ms=1000,
        width=1024,
        height=576,
        fps=12,
        checksum="sha256:video",
        metadata={
            "studio_panel": {
                "actual_camera_view": "panel_two_shot",
                "camera_composition": "native_scene_camera",
                "speaker_face_region_px": {"x": 1, "y": 2, "width": 120, "height": 110},
                "framed_participant_ids": ["grok", "mistral"],
            }
        },
    )

    ComfyUiService()._apply_visual_result(asset, result, submitted_by="tester")

    assert asset.status == "completed"
    assert asset.generation_metadata.get("native_camera_coverage_rejected") is not True


@pytest.mark.asyncio
async def test_b1_studio_panel_reuses_approved_seated_character_provenance() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    seated_character = Asset(
        episode_id=episode.id,
        asset_type=AssetType.image,
        source_entity_type="participant_profile",
        source_entity_id="host",
        status="completed",
        generation_metadata={
            "visual_role": "studio_seated_character",
            "approval_status": "approved",
            "seated_character": {
                "seated_reference_artifact_id": "upload_host_seated"
            },
            "b1_upload_references": {
                "studio_reference": {
                    "id": "upload_studio",
                    "sha256": "studio-sha256",
                },
                "host": {
                    "portrait": "upload_host_portrait",
                    "full_body": "upload_host_full_body",
                },
            },
        },
    )
    episode.assets.append(seated_character)
    asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id=f"{episode.id}:panel:establishing_wide:host:solo",
        generation_metadata={
            "prompt_inputs": {
                "show_scene_reference_image_uri": "object://dialecticore/studio/reference.png",
                "camera_view": "speaker_medium",
                "seating_plan": {"host": 1},
                "panel_participants": [
                    {
                        "participant_id": "host",
                        "seat": 1,
                        "seated_character_asset_id": str(seated_character.id),
                    }
                ],
            }
        },
    )
    workflow = next(
        workflow
        for workflow in default_comfyui_workflows()
        if workflow.workflow_type == "studio_panel_shot"
    )

    payload, metadata = await ComfyUiService()._b1_studio_panel_payload(
        workflow=workflow,
        episode=episode,
        transcript=TranscriptVersion(
            episode_id=episode.id,
            type=TranscriptType.broadcast,
            language="en",
        ),
        asset=asset,
    )

    assert payload["input"]["studio_reference_artifact_id"] == "upload_studio"
    assert payload["input"]["participants"] == [
        {
            "participant_id": "host",
            "seat": 1,
            "portrait_artifact_id": "upload_host_portrait",
            "full_body_artifact_id": "upload_host_full_body",
            "seated_reference_artifact_id": "upload_host_seated",
        }
    ]
    assert payload["input"]["stature_reference_participant_id"] == "host"
    assert payload["input"]["camera"] == {"view": "establishing_wide", "action": "cut"}
    assert payload["input"]["width"] == 1280
    assert payload["input"]["height"] == 720
    assert isinstance(payload["input"]["seed"], int)
    assert 0 <= payload["input"]["seed"] <= 2_147_483_647
    assert metadata["studio_panel"]["requested_camera_view"] == "speaker_medium"
    assert metadata["studio_panel"]["stature_reference_participant_id"] == "host"
    assert metadata["b1_upload_references"]["host"]["seated_reference"] == (
        "upload_host_seated"
    )


@pytest.mark.asyncio
async def test_b1_seated_character_uploads_private_identity_and_studio_inputs(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    object_store = create_object_store(settings)
    alpha_image = png_rgba(
        2,
        2,
        [(40, 90, 180, 128), (40, 90, 180, 128), (40, 90, 180, 128), (40, 90, 180, 128)],
    )
    studio_uri = object_store.put_bytes("studio/reference.png", alpha_image, "image/png").uri
    portrait_uri = object_store.put_bytes(
        "profiles/host/portrait.png", alpha_image, "image/png"
    ).uri
    full_body_uri = object_store.put_bytes(
        "profiles/host/full-body.png", alpha_image, "image/png"
    ).uri
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.image,
        source_entity_type="participant_profile",
        source_entity_id="host",
        generation_metadata={
            "prompt_inputs": {
                "participant_id": "host",
                "seat": 1,
                "portrait_reference_image_uri": portrait_uri,
                "full_body_reference_image_uri": full_body_uri,
                "show_scene_reference_image_uri": studio_uri,
            }
        },
    )
    uploaded_fields: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/media/uploads"
        assert request.headers["content-type"] == "image/png"
        field = request.headers["x-b1-field"]
        uploaded_fields.append(field)
        return httpx.Response(200, json={"reference": {"id": f"upload_{field}"}})

    workflow = next(
        workflow
        for workflow in default_comfyui_workflows()
        if workflow.workflow_type == "studio_seated_character"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload, metadata = await ComfyUiService(settings)._b1_seated_character_payload(
            client=client,
            api_base="https://api.ai.b1.germering",
            workflow=workflow,
            episode=episode,
            asset=asset,
        )

    assert uploaded_fields == ["studio_reference", "portrait", "full_body"]
    assert payload["operation"] == "studio-seated-character"
    assert payload["model"] == "studio-seated-character-p40"
    assert payload["runtime_policy"] == "any"
    assert payload["input"] == {
        "participant_id": "host",
        "portrait_artifact_id": "upload_portrait",
        "full_body_artifact_id": "upload_full_body",
        "studio_reference_artifact_id": "upload_studio_reference",
        "seat": 1,
        "pose": "neutral_seated",
        "camera_view": "establishing_wide",
        "camera_angle": "front_three_quarter",
        "width": 1280,
        "height": 720,
        "seed": payload["input"]["seed"],
    }
    assert metadata["b1_upload_references"]["studio_reference"]["repacked_for_b1"] is False
    assert metadata["b1_upload_references"]["host"]["portrait_repacked_for_b1"] is False
    assert metadata["b1_upload_references"]["host"]["full_body_repacked_for_b1"] is False


@pytest.mark.asyncio
async def test_visual_sync_never_marks_planned_dependency_assets_failed(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target = next(asset for asset in planned.assets if asset.asset_type == AssetType.broll)
    dependency = next(
        asset
        for asset in planned.assets
        if asset.id != target.id
        and asset.generation_metadata.get("visual_role") == "video_primary"
    )
    target.status = "submitted"
    target.generation_metadata = {
        **target.generation_metadata,
        "remote_job_id": "visual-job-running",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/history/visual-job-running"
        return httpx.Response(
            200,
            json={"status": "running", "prompt_id": "visual-job-running"},
        )

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]

    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))
    synced = await service.sync_visual_results(
        planned,
        VisualResultSyncRequest(
            asset_ids=[target.id, dependency.id],
            include_completed=True,
            user_id="tester",
        ),
        endpoints=[endpoint],
        workflows=workflows,
    )

    assert next(asset for asset in synced.assets if asset.id == target.id).status == "running"
    assert next(asset for asset in synced.assets if asset.id == dependency.id).status == "planned"


def test_b1_managed_media_result_retains_reusable_studio_panel_reference() -> None:
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id="episode:panel",
        generation_metadata={"adapter": "b1_managed_media"},
    )
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
    )

    result = ComfyUiService()._visual_result_from_payload(
        endpoint,
        asset,
        {
            "id": "job-panel-1",
            "state": "completed",
            "studio_panel": {
                "camera_view": "speaker_medium",
                "scene_artifact_id": "upload-b1-panel-scene",
                "seat_map": [{"participant_id": "host", "seat": 2}],
                "wall_screen_quad": [{"x": 0.24, "y": 0.12}],
            },
        },
        default_job_id="job-panel-1",
        default_status="running",
    )
    ComfyUiService()._apply_visual_result(asset, result, synced_by="tester")

    assert asset.generation_metadata["studio_panel"]["scene_artifact_id"] == (
        "upload-b1-panel-scene"
    )
    assert asset.generation_metadata["provider_studio_panel_seat_map"] == [
        {"participant_id": "host", "seat": 2}
    ]


def test_studio_panel_master_requires_review_before_dependent_clips_generate() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    master = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id=f"{episode.id}:panel:establishing_wide",
        status="completed",
        generation_metadata={
            "adapter": "b1_managed_media",
            "visual_role": "studio_panel_keyframe",
            "studio_panel": {
                "scene_artifact_id": "upload-b1-panel-master",
                "seat_map": [{"participant_id": "host", "seat": 1}],
            },
        },
    )
    primary = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-host",
        generation_metadata={
            "visual_role": "video_primary",
            "prompt_inputs": {"depends_on_asset_ids": [str(master.id)]},
        },
    )
    episode.assets.extend([master, primary])
    service = ComfyUiService()

    assert service._visual_dependency_state(episode, primary) == {
        "reason": "studio_panel_review_required",
        "pending_asset_ids": [],
        "failed_asset_ids": [],
        "review_required_asset_ids": [str(master.id)],
        "rejected_asset_ids": [],
    }

    service.review_studio_panel_keyframe(
        episode,
        str(master.id),
        StudioPanelReviewRequest(decision="approved", user_id="tester"),
    )

    assert master.generation_metadata["approval_status"] == "approved"
    assert service._visual_dependency_state(episode, primary) is None
    assert episode.audit_events[-1].event_type == "visual.studio_panel.reviewed"


@pytest.mark.asyncio
async def test_seated_character_approval_calls_b1_before_unblocking_panel(
    monkeypatch,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "operator-token")
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    plate = Asset(
        episode_id=episode.id,
        asset_type=AssetType.image,
        source_entity_type="participant_profile",
        source_entity_id="host",
        status="completed",
        generation_metadata={
            "adapter": "b1_managed_media",
            "visual_role": "studio_seated_character",
            "comfyui_workflow_id": "workflow-studio-seated-character-p40-v2",
            "managed_media_api_base": "https://api.ai.b1.germering",
            "remote_job_id": "job_plate_1",
        },
    )
    panel = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id=f"{episode.id}:panel",
        generation_metadata={
            "visual_role": "studio_panel_keyframe",
            "prompt_inputs": {"depends_on_asset_ids": [str(plate.id)]},
        },
    )
    episode.assets.extend([plate, panel])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/admin/jobs/job_plate_1/approve-seated-character"
        assert request.headers["authorization"] == "Bearer operator-token"
        return httpx.Response(
            200,
            json={
                "id": "job_plate_1",
                "seated_character": {
                    "seated_reference_artifact_id": "upload_host_seated"
                },
            },
        )

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
    )
    service = ComfyUiService(transport=httpx.MockTransport(handler))

    dependency_state = service._visual_dependency_state(episode, panel)
    assert dependency_state is not None
    assert dependency_state["reason"] == "seated_character_review_required"

    await service.review_seated_character(
        episode,
        str(plate.id),
        SeatedCharacterReviewRequest(decision="approved", user_id="tester"),
        endpoints=[endpoint],
        workflows=default_comfyui_workflows(),
    )

    assert len(requests) == 1
    assert plate.generation_metadata["approval_status"] == "approved"
    assert plate.generation_metadata["b1_approval_response"]["seated_character"][
        "seated_reference_artifact_id"
    ] == "upload_host_seated"
    assert service._visual_dependency_state(episode, panel) is None
    assert episode.audit_events[-1].event_type == "visual.seated_character.reviewed"


@pytest.mark.asyncio
async def test_b1_directed_studio_media_uses_private_image_upload_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "b1-token")
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(
        asset
        for asset in planned.assets
        if asset.asset_type == AssetType.studio_scene
        and asset.generation_metadata.get("visual_role") == "studio_scene"
    )
    object_store = create_object_store(settings)
    studio_reference = object_store.put_bytes(
        "show-media/test/studio-reference.png",
        png_rgba(1, 1, [(40, 90, 180, 255)]),
        "image/png",
    )
    target_asset.generation_metadata["prompt_inputs"] = {
        **target_asset.generation_metadata.get("prompt_inputs", {}),
        "show_scene_reference_image_uri": studio_reference.uri,
    }
    captured_payload: dict = {}
    upload_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer b1-token"
        if request.url.path == "/v1/media/uploads":
            upload_paths.append(request.url.path)
            assert request.headers["x-b1-field"] == "image"
            assert request.headers["content-type"] == "image/png"
            assert request.content.startswith(b"\x89PNG")
            return httpx.Response(200, json={"reference": {"id": "upload-studio"}})
        assert request.url.path == "/v1/media/jobs"
        captured_payload.update(json_from_request(request))
        return httpx.Response(
            202,
            json={
                "b1_job_id": "job-studio-1",
                "b1_status": "queued",
                "links": {"self": "/v1/media/jobs/job-studio-1"},
            },
        )

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Managed Media",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"remote_nodes_api_base": "https://api.ai.b1.germering"},
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "b1-comfyui"})
        for workflow in default_comfyui_workflows()
    ]
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))
    submitted = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(
            asset_ids=[target_asset.id],
            user_id="tester",
            fallback_on_failure=False,
        ),
        endpoints=[endpoint],
        workflows=workflows,
    )

    assert upload_paths == ["/v1/media/uploads"]
    assert captured_payload["model"] == "video-image"
    assert captured_payload["operation"] == "image-to-video"
    assert captured_payload["input"]["source_image_artifact_id"] == "upload-studio"
    assert "source_image" not in captured_payload["input"]
    assert captured_payload["input"]["width"] == 256
    assert captured_payload["input"]["height"] == 256
    assert captured_payload["input"]["fps"] == 12
    assert captured_payload["input"]["duration_ms"] == 2500
    assert captured_payload["input"]["frame_count"] == 30
    assert "steps" not in captured_payload["input"]
    assert "cfg" not in captured_payload["input"]
    assert "object://" not in str(captured_payload)
    assert not {
        "asset_id",
        "source_entity_type",
        "source_entity_id",
        "visual_role",
        "shot_type",
        "workflow_id",
        "workflow_type",
        "workflow_version",
    }.intersection(captured_payload["input"])
    submitted_asset = next(asset for asset in submitted.assets if asset.id == target_asset.id)
    assert submitted_asset.status == "submitted"
    assert submitted_asset.generation_metadata["adapter"] == "b1_managed_media"
    reference_metadata = submitted_asset.generation_metadata["b1_upload_references"]
    assert reference_metadata["transport"] == "authenticated_staged_upload"
    assert reference_metadata["input_fields"] == ["source_image_artifact_id"]
    assert reference_metadata["source_fields"]["show_scene_reference_image_uri"]["count"] == 1


@pytest.mark.asyncio
async def test_b1_group_cutaway_composes_private_character_references(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    service = ComfyUiService(settings)
    target_asset = next(
        asset
        for asset in planned.assets
        if asset.generation_metadata.get("visual_role") == "studio_group_cutaway"
    )
    target_request = VisualGenerationRequest(asset_ids=[target_asset.id])
    transcript = service._target_transcript(planned, target_request)
    assert service._target_visual_assets(planned, transcript, target_request) == [target_asset]
    object_store = create_object_store(settings)
    references = [
        object_store.put_bytes(
            f"visual-profiles/test/group-{index}.png",
            png_rgba(1, 1, [(40 + index, 90, 180, 255)]),
            "image/png",
        ).uri
        for index in range(2)
    ]
    payload, content_type, source_field, source_count = service._b1_video_image_source(
        asset=target_asset,
        references={"group_reference_image_uris": references},
    )

    assert payload.startswith(b"\x89PNG")
    assert content_type == "image/png"
    assert ComfyUiService(settings)._png_dimensions(payload) == (256, 256)
    assert source_field == "group_reference_image_uris"
    assert source_count == 2


def test_b1_group_cutaway_handles_three_portrait_references(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = ComfyUiService(settings)
    object_store = create_object_store(settings)
    references = [
        object_store.put_bytes(
            f"visual-profiles/test/portrait-{index}.png",
            png_rgba(2, 3, [(40 + index, 90, 180, 255)] * 6),
            "image/png",
        ).uri
        for index in range(3)
    ]

    payload, content_type = service._compose_b1_group_source_image(references)

    assert payload.startswith(b"\xff\xd8")
    assert content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_global_visual_qc_ignores_unproduced_optional_broll(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    service = ComfyUiService(settings)
    primary_asset = next(
        asset
        for asset in planned.assets
        if asset.generation_metadata.get("visual_role") == "video_primary"
    )
    broll_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.broll)
    for asset in planned.assets:
        if asset.id not in {primary_asset.id, broll_asset.id}:
            asset.status = "replaced"
    broll_asset.status = "failed"
    completed = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(
            asset_ids=[primary_asset.id],
            local_fallback_only=True,
            user_id="tester",
        ),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
        visual_profiles=default_visual_profiles(),
    )

    checked = service.run_visual_quality(
        completed,
        VisualQualityRequest(user_id="tester"),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )

    media_qc = [
        result
        for result in checked.quality_results
        if result.check_type == "visual_media_integrity"
    ][-1]
    assert media_qc.status == "warning"
    assert media_qc.details["checked_visual_asset_count"] == 1
    assert media_qc.details["skipped_optional_broll_asset_ids"] == [str(broll_asset.id)]
    assert "visual_asset_not_completed" not in {
        issue["issue"] for issue in media_qc.details["issues"]
    }


@pytest.mark.asyncio
async def test_b1_managed_media_retry_clears_completed_fallback_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "b1-token")
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    fallback_generated = await ComfyUiService(settings).generate_visual_assets(
        planned,
        VisualGenerationRequest(
            asset_ids=[target_asset.id],
            user_id="tester",
            local_fallback_only=True,
        ),
        endpoints=default_comfyui_endpoints(),
        workflows=default_comfyui_workflows(),
    )
    fallback_asset = next(
        asset for asset in fallback_generated.assets if asset.id == target_asset.id
    )
    assert fallback_asset.status == "completed"
    assert fallback_asset.storage_uri
    assert fallback_asset.checksum
    assert fallback_asset.generation_metadata["fallback_visual"] is True
    prepare_b1_lipsync_inputs(settings, fallback_generated, fallback_asset)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "api.ai.b1.germering"
        if request.url.path == "/v1/media/uploads":
            field = request.headers["x-b1-field"]
            return httpx.Response(200, json={"reference": f"upload-{field}"})
        assert request.url.path == "/v1/media/jobs"
        return httpx.Response(
            202,
            json={
                "object": "b1.async_job",
                "id": "job-managed-retry",
                "state": "queued",
            },
        )

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"remote_nodes_api_base": "https://api.ai.b1.germering"},
    )
    workflow = default_comfyui_workflows()[0].model_copy(
        update={"comfyui_endpoint_id": "b1-comfyui"}
    )
    retried = await ComfyUiService(
        settings,
        transport=httpx.MockTransport(handler),
    ).generate_visual_assets(
        fallback_generated,
        VisualGenerationRequest(
            asset_ids=[target_asset.id],
            user_id="tester",
            regenerate=True,
            fallback_on_failure=False,
        ),
        endpoints=[endpoint],
        workflows=[workflow, *default_comfyui_workflows()[1:]],
    )

    retried_asset = next(asset for asset in retried.assets if asset.id == target_asset.id)
    assert retried_asset.status == "submitted"
    assert retried_asset.storage_uri is None
    assert retried_asset.checksum is None
    assert retried_asset.generation_metadata["adapter"] == "b1_managed_media"
    assert retried_asset.generation_metadata["remote_job_id"] == "job-managed-retry"
    assert "fallback_visual" not in retried_asset.generation_metadata
    assert "fallback_reason" not in retried_asset.generation_metadata
    assert retried_asset.generation_metadata["remote_job_cancellation_required"] is True


@pytest.mark.asyncio
async def test_visual_generation_without_fallback_clears_prior_fallback_artifact(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    fallback_generated = await ComfyUiService(settings).generate_visual_assets(
        planned,
        VisualGenerationRequest(
            asset_ids=[target_asset.id],
            user_id="tester",
            local_fallback_only=True,
        ),
        endpoints=[],
        workflows=default_comfyui_workflows(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/prompt"
        return httpx.Response(503, json={"status": "failed", "error": "busy"})

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]
    failed = await ComfyUiService(
        settings,
        transport=httpx.MockTransport(handler),
    ).generate_visual_assets(
        fallback_generated,
        VisualGenerationRequest(
            asset_ids=[target_asset.id],
            user_id="tester",
            regenerate=True,
            fallback_on_failure=False,
        ),
        endpoints=[endpoint],
        workflows=workflows,
    )

    failed_asset = next(asset for asset in failed.assets if asset.id == target_asset.id)
    assert failed_asset.status == "failed"
    assert failed_asset.storage_uri is None
    assert failed_asset.mime_type is None
    assert failed_asset.checksum is None
    assert "fallback_visual" not in failed_asset.generation_metadata
    assert "media_probe" not in failed_asset.generation_metadata
    assert "503 Service Unavailable" in failed_asset.generation_metadata["failure"]


@pytest.mark.asyncio
async def test_b1_managed_media_sync_downloads_api_hub_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "b1-token")
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    target_asset.status = "submitted"
    target_asset.generation_metadata = {
        **target_asset.generation_metadata,
        "adapter": "comfyui",
        "managed_media_api_base": "https://api.ai.b1.germering",
        "remote_job_id": "job-managed-2",
    }
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.method == "GET" and request.url.path == "/v1/media/jobs/job-managed-2":
            return httpx.Response(
                200,
                json={
                    "id": "job-managed-2",
                    "state": "completed",
                    "artifacts": [
                        {
                            "url": "/artifacts/media/job-managed-2/output.mp4",
                            "mime_type": "video/mp4",
                            "sha256": "provider-sha",
                        }
                    ],
                    "lip_sync": {
                        "mode": "audio_driven",
                        "measured_offset_ms": 0,
                        "duration_ms": 1000,
                        "fps": 12,
                    },
                    "links": {"self": "/v1/media/jobs/job-managed-2"},
                },
            )
        if (
            request.method == "GET"
            and request.url.path == "/artifacts/media/job-managed-2/output.mp4"
        ):
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"mp4")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"remote_nodes_api_base": "https://api.ai.b1.germering"},
    )
    workflow = default_comfyui_workflows()[0].model_copy(
        update={"comfyui_endpoint_id": "b1-comfyui"}
    )
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    synced = await service.sync_visual_results(
        planned,
        VisualResultSyncRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=[workflow, *default_comfyui_workflows()[1:]],
    )

    synced_asset = next(asset for asset in synced.assets if asset.id == target_asset.id)
    assert synced_asset.status == "completed"
    assert synced_asset.storage_uri is not None
    assert synced_asset.storage_uri.startswith("object://")
    assert synced_asset.mime_type == "video/mp4"
    assert synced_asset.generation_metadata["adapter"] == "b1_managed_media"
    assert synced_asset.generation_metadata["managed_media_api_base"] == (
        "https://api.ai.b1.germering"
    )
    assert synced_asset.generation_metadata["remote_job_id"] == "job-managed-2"
    assert synced_asset.generation_metadata["remote_status"] == "completed"
    assert synced_asset.generation_metadata["lip_sync_mode"] == "audio_driven"
    assert synced_asset.generation_metadata["provider_lip_sync_offset_ms"] == 0
    assert synced_asset.generation_metadata["provider_response"]["artifacts"][0]["url"] == (
        "/artifacts/media/job-managed-2/output.mp4"
    )
    assert ("GET", "/v1/media/jobs/job-managed-2", "Bearer b1-token") in calls
    assert (
        "GET",
        "/artifacts/media/job-managed-2/output.mp4",
        "Bearer b1-token",
    ) in calls


def test_b1_managed_media_failure_retains_provider_evidence() -> None:
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        status="running",
        generation_metadata={
            "adapter": "b1_managed_media",
            "managed_media_api_base": "https://api.ai.b1.germering",
        },
    )
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
    )

    result = ComfyUiService()._visual_result_from_payload(
        endpoint,
        asset,
        {
            "id": "job-failed",
            "state": "failed",
            "failure_category": "gpu_runner_error",
            "failure_message": "MuseTalk did not create a non-empty MP4",
        },
        default_job_id="job-failed",
        default_status="running",
    )
    ComfyUiService()._apply_visual_result(asset, result, synced_by="tester")

    assert asset.status == "failed"
    assert asset.generation_metadata["adapter"] == "b1_managed_media"
    assert asset.generation_metadata["provider_failure_category"] == "gpu_runner_error"
    assert asset.generation_metadata["failure"] == "MuseTalk did not create a non-empty MP4"


def test_b1_managed_media_completion_clears_prior_failure_and_keeps_lipsync_evidence() -> None:
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.video,
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        status="failed",
        generation_metadata={
            "adapter": "b1_managed_media",
            "managed_media_api_base": "https://api.ai.b1.germering",
            "failure": "previous B1 runtime failure",
            "provider_failure_category": "gpu_runner_error",
            "provider_failure_message": "previous B1 runtime failure",
        },
    )
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
    )

    result = ComfyUiService()._visual_result_from_payload(
        endpoint,
        asset,
        {
            "id": "job-completed",
            "state": "completed",
            "lip_sync": {
                "mode": "audio_driven",
                "audio_sha256": "a" * 64,
                "timing_sha256": "b" * 64,
                "measured_offset_ms": 0,
                "duration_ms": 13040,
                "fps": 12,
                "backend": "b1-musetalk-v1.5",
                "musetalk_commit": "0a89dec",
            },
            "performance": {
                "mode": "audio_driven_character_performance",
                "applied": True,
            },
        },
        default_job_id="job-completed",
        default_status="running",
    )
    ComfyUiService()._apply_visual_result(asset, result, synced_by="tester")

    assert asset.status == "completed"
    assert asset.generation_metadata["remote_status"] == "completed"
    assert asset.generation_metadata["provider_audio_sha256"] == "a" * 64
    assert asset.generation_metadata["provider_timing_sha256"] == "b" * 64
    assert asset.generation_metadata["provider_lip_sync_duration_ms"] == 13040
    assert asset.generation_metadata["provider_lip_sync_fps"] == 12.0
    assert asset.generation_metadata["provider_musetalk_commit"] == "0a89dec"
    assert asset.generation_metadata["provider_performance_mode"] == (
        "audio_driven_character_performance"
    )
    assert asset.generation_metadata["provider_performance_applied"] is True
    assert "failure" not in asset.generation_metadata
    assert "provider_failure_category" not in asset.generation_metadata
    assert "provider_failure_message" not in asset.generation_metadata


def test_b1_managed_media_resubmission_clears_prior_failure() -> None:
    asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.studio_scene,
        language="de",
        source_entity_type="episode",
        source_entity_id="episode-1",
        status="failed",
        generation_metadata={
            "adapter": "b1_managed_media",
            "failure": "previous B1 request failure",
            "failed_at": "2026-08-02T12:00:00+00:00",
            "ready_for_retry": True,
            "retry_exhausted": False,
            "provider_failure_category": "gpu_runner_error",
            "provider_failure_message": "previous B1 request failure",
        },
    )
    endpoint = ComfyUiEndpoint(
        id="b1-comfyui",
        name="B1 Native ComfyUI",
        adapter_type="comfyui_http",
        base_url="https://comfy.ai.b1.germering",
    )

    result = ComfyUiService()._visual_result_from_payload(
        endpoint,
        asset,
        {"id": "job-retry", "state": "queued"},
        default_job_id="job-retry",
        default_status="submitted",
    )
    ComfyUiService()._apply_visual_result(asset, result, submitted_by="tester")

    assert asset.status == "submitted"
    assert asset.generation_metadata["remote_job_id"] == "job-retry"
    for key in (
        "failure",
        "failed_at",
        "ready_for_retry",
        "retry_exhausted",
        "provider_failure_category",
        "provider_failure_message",
    ):
        assert key not in asset.generation_metadata


@pytest.mark.asyncio
async def test_remote_visual_generation_can_cancel_submitted_job(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    planned = await planned_visual_episode(settings)
    target_asset = next(asset for asset in planned.assets if asset.asset_type == AssetType.video)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/prompt":
            return httpx.Response(200, json={"status": "queued", "prompt_id": "visual-job-2"})
        if request.method == "DELETE" and request.url.path == "/queue/visual-job-2":
            return httpx.Response(
                200,
                json={
                    "status": "cancelled",
                    "accessToken": "leaked-cancel-token",
                    "nested": {"clientSecret": "leaked-cancel-secret"},
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    endpoint = ComfyUiEndpoint(
        id="comfyui-remote",
        name="ComfyUI Remote",
        adapter_type="comfyui_http",
        base_url="https://comfyui.example.test",
    )
    workflows = [
        workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
        for workflow in default_comfyui_workflows()
    ]
    service = ComfyUiService(settings, transport=httpx.MockTransport(handler))

    submitted = await service.generate_visual_assets(
        planned,
        VisualGenerationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )
    cancelled = await service.cancel_visual_jobs(
        submitted,
        VisualCancellationRequest(asset_ids=[target_asset.id], user_id="tester"),
        endpoints=[endpoint],
        workflows=workflows,
    )

    cancelled_asset = next(asset for asset in cancelled.assets if asset.id == target_asset.id)
    assert cancelled_asset.status == "planned"
    assert cancelled_asset.generation_metadata["cancelled_remote_job_id"] == "visual-job-2"
    assert cancelled_asset.generation_metadata["remote_cancelled"] is True
    assert cancelled_asset.generation_metadata["remote_cancel_response"]["accessToken"] == (
        "[redacted]"
    )
    assert cancelled_asset.generation_metadata["remote_cancel_response"]["nested"][
        "clientSecret"
    ] == "[redacted]"
    metadata_json = json.dumps(cancelled_asset.generation_metadata, sort_keys=True)
    assert "leaked-cancel-token" not in metadata_json
    assert "leaked-cancel-secret" not in metadata_json
    assert ("DELETE", "/queue/visual-job-2") in calls
    assert cancelled.audit_events[-1].event_type == "visual.jobs.cancelled"


def json_from_request(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())
