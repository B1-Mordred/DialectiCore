import json
from pathlib import Path

import httpx
import pytest
from app.core.config import Settings
from app.services.b1_managed_media_smoke_service import B1ManagedMediaSmokeService


@pytest.mark.asyncio
async def test_b1_managed_media_smoke_service_writes_pass_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "test-token")
    ca_file = tmp_path / "runtime" / "certificates" / "b1-ai-hub-caddy-root.crt"
    ca_file.parent.mkdir(parents=True)
    ca_file.write_text("cert", encoding="utf-8")
    evidence_path = tmp_path / "smoke.json"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "job-pass",
                    "state": "queued",
                    "model": "image-default",
                    "operation": "image-generation",
                    "modality": "image",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "job-pass",
                "state": "completed",
                "stage": "completed",
                "artifacts": [{"id": "artifact-1"}],
            },
        )

    service = B1ManagedMediaSmokeService(
        Settings(runtime_state_path=str(tmp_path / "runtime")),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_smoke(
        api_base="https://api.test",
        poll_interval_seconds=0,
        evidence_output=str(evidence_path),
        requirements_output=None,
    )

    assert result["exit_code"] == 0
    assert result["result"]["status"] == "pass"
    assert result["result"]["job_id"] == "job-pass"
    assert result["result"]["terminal"]["artifact_count"] == 1
    assert result["result"]["evidence_file"]["path"] == str(evidence_path)
    assert evidence_path.exists()
    assert str(requests[0].url) == "https://api.test/v1/images/generations"
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert requests[0].headers["idempotency-key"].startswith(
        "dialecticore-b1-media-smoke-"
    )
    assert json.loads(requests[0].content) == {
        "model": "image-default",
        "prompt": "small neutral studio lighting test card, no text",
        "n": 1,
        "size": "128x128",
        "negative_prompt": "text, watermark, logo",
        "steps": 1,
        "cfg": 1.0,
        "seed": 7,
    }
    assert str(requests[1].url) == "https://api.test/v1/media/jobs/job-pass"


@pytest.mark.asyncio
async def test_b1_managed_media_smoke_service_appends_requirements_on_runner_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "test-token")
    ca_file = tmp_path / "runtime" / "certificates" / "b1-ai-hub-caddy-root.crt"
    ca_file.parent.mkdir(parents=True)
    ca_file.write_text("cert", encoding="utf-8")
    requirements_path = tmp_path / "media-requirements.md"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) in {
            "https://api.test/v1/media/jobs",
            "https://api.test/v1/media/jobs/job-fail",
        }
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-fail", "state": "queued"})
        return httpx.Response(
            200,
            json={
                "id": "job-fail",
                "state": "failed",
                "stage": "failed",
                "failure_category": "gpu_runner_error",
                "failure_message": "ValueError",
                "native_prompt_id": "prompt-1",
                "artifacts": [],
            },
        )

    service = B1ManagedMediaSmokeService(
        Settings(runtime_state_path=str(tmp_path / "runtime")),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_smoke(
        api_base="https://api.test",
        model="video-image",
        poll_interval_seconds=0,
        evidence_output=str(tmp_path / "smoke.json"),
        requirements_output=str(requirements_path),
    )

    assert result["exit_code"] == 2
    assert result["result"]["status"] == "runner_failed"
    assert result["result"]["requirements_update"] == {
        "path": str(requirements_path),
        "appended": True,
    }
    requirements = requirements_path.read_text(encoding="utf-8")
    assert "POST /api/v1/system/b1-managed-media-smoke" in requirements
    assert "model alias: `video-image`" in requirements
    assert "failure category: `gpu_runner_error`" in requirements
    assert "`video-image`: Wan 2.1 VACE 1.3B" in requirements


@pytest.mark.asyncio
async def test_b1_managed_media_smoke_treats_scheduler_conflict_as_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("B1_API_KEY", "test-token")
    ca_file = tmp_path / "runtime" / "certificates" / "b1-ai-hub-caddy-root.crt"
    ca_file.parent.mkdir(parents=True)
    ca_file.write_text("cert", encoding="utf-8")
    requirements_path = tmp_path / "media-requirements.md"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"reason": "gpu_lease_unavailable", "retry_after_seconds": 30},
            request=request,
        )

    service = B1ManagedMediaSmokeService(
        Settings(runtime_state_path=str(tmp_path / "runtime")),
        transport=httpx.MockTransport(handler),
    )

    result = await service.run_smoke(
        api_base="https://api.test",
        evidence_output=str(tmp_path / "smoke.json"),
        requirements_output=str(requirements_path),
    )

    assert result["exit_code"] == 0
    assert result["result"]["status"] == "busy"
    assert result["result"]["busy"] == {
        "status_code": 409,
        "reason": "gpu_lease_unavailable",
        "retry_after_seconds": 30,
    }
    assert result["result"].get("requirements_update") is None
    assert not requirements_path.exists()
