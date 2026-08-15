import httpx
import pytest
from app.domain.enums import ProviderType
from app.domain.schemas import ModelEndpoint
from app.services.model_endpoint_service import ModelEndpointService


@pytest.mark.asyncio
async def test_mock_model_endpoint_health_reports_execution_capabilities() -> None:
    service = ModelEndpointService()
    endpoint = ModelEndpoint(
        id="mock",
        name="Mock",
        provider_type=ProviderType.mock,
    )

    checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "healthy"
    assert checked.capabilities["turn_generation"] is True
    assert checked.capabilities["structured_turn_output"] is True
    assert checked.capabilities["deterministic"] is True


@pytest.mark.asyncio
async def test_openai_compatible_model_health_reads_model_listing(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_HEALTH_TOKEN", "health-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        seen["http_referer"] = request.headers.get("http-referer")
        seen["x_title"] = request.headers.get("x-title")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-test"},
                    {"id": "gpt-reviewer"},
                ],
                "capabilities": {
                    "batch": False,
                    "accessToken": "leaked-model-health-token",
                    "nested": {"clientSecret": "leaked-model-health-secret"},
                },
            },
        )

    service = ModelEndpointService(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openai-compatible",
        name="OpenAI Compatible",
        provider_type=ProviderType.openai_compatible,
        base_url="https://models.example.test/v1",
        credential_reference="env:MODEL_HEALTH_TOKEN",
        capabilities={
            "site_url": "https://dialecticore.example",
            "app_title": "DialectiCore",
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    assert seen["path"] == "/v1/models"
    assert seen["authorization"] == "Bearer health-token"
    assert seen["http_referer"] == "https://dialecticore.example"
    assert seen["x_title"] == "DialectiCore"
    assert checked.health_status == "healthy"
    assert checked.capabilities["chat_completions"] is True
    assert checked.capabilities["json_schema_response"] is True
    assert checked.capabilities["model_listing"] is True
    assert checked.capabilities["model_count"] == 2
    assert checked.capabilities["model_ids"] == ["gpt-test", "gpt-reviewer"]
    assert checked.capabilities["batch"] is False
    assert checked.capabilities["accessToken"] == "[redacted]"
    assert checked.capabilities["nested"]["clientSecret"] == "[redacted]"
    capabilities_json = str(checked.capabilities)
    assert "leaked-model-health-token" not in capabilities_json
    assert "leaked-model-health-secret" not in capabilities_json


@pytest.mark.asyncio
async def test_model_endpoint_health_uses_file_secret_reference(tmp_path) -> None:
    secret_path = tmp_path / "model-token"
    secret_path.write_text("file-health-token\n", encoding="utf-8")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "gpt-file-secret"}]})

    service = ModelEndpointService(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openai-compatible",
        name="OpenAI Compatible",
        provider_type=ProviderType.openai_compatible,
        base_url="https://models.example.test/v1",
        credential_reference=f"file:{secret_path}",
    )

    checked = await service.check_endpoint_health(endpoint)

    assert seen["authorization"] == "Bearer file-health-token"
    assert checked.credential_reference == f"file:{secret_path}"
    assert checked.capabilities["model_ids"] == ["gpt-file-secret"]


@pytest.mark.asyncio
async def test_anthropic_compatible_model_health_uses_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_HEALTH_TOKEN", "anthropic-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["x_api_key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"data": [{"id": "claude-test"}]})

    service = ModelEndpointService(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="anthropic-compatible",
        name="Anthropic Compatible",
        provider_type=ProviderType.anthropic_compatible,
        base_url="https://anthropic.example.test/v1",
        credential_reference="env:ANTHROPIC_HEALTH_TOKEN",
    )

    checked = await service.check_endpoint_health(endpoint)

    assert seen["path"] == "/v1/models"
    assert seen["x_api_key"] == "anthropic-token"
    assert checked.health_status == "healthy"
    assert checked.capabilities["messages"] is True
    assert checked.capabilities["text_content_blocks"] is True
    assert checked.capabilities["model_ids"] == ["claude-test"]


@pytest.mark.asyncio
async def test_generic_http_model_health_uses_configured_path_and_raw_auth(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GENERIC_HEALTH_TOKEN", "Token raw")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"capabilities": {"custom_turns": True}})

    service = ModelEndpointService(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="generic-http",
        name="Generic HTTP",
        provider_type=ProviderType.generic_http,
        base_url="https://generic.example.test/api",
        credential_reference="env:GENERIC_HEALTH_TOKEN",
        capabilities={
            "health_path": "ready",
            "authorization_scheme": "raw",
        },
    )

    checked = await service.check_endpoint_health(endpoint)

    assert seen["path"] == "/api/ready"
    assert seen["authorization"] == "Token raw"
    assert checked.health_status == "healthy"
    assert checked.capabilities["generic_turn_request"] is True
    assert checked.capabilities["custom_turns"] is True


@pytest.mark.asyncio
async def test_remote_model_endpoint_without_base_url_is_unconfigured() -> None:
    service = ModelEndpointService()
    endpoint = ModelEndpoint(
        id="ollama",
        name="Ollama",
        provider_type=ProviderType.ollama,
    )

    checked = await service.check_endpoint_health(endpoint)

    assert checked.health_status == "unconfigured"
