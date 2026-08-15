import json
from dataclasses import replace

import httpx
import pytest
from app.domain.enums import ParticipantType, ProviderType
from app.domain.schemas import (
    DiscussionPromptTemplate,
    ModelEndpoint,
    ParticipantMemory,
    ParticipantProfile,
    SamplingSettings,
)
from app.services.model_gateway import ModelGateway, StructuredTurnOutputError, TurnContext
from app.services.prompt_templates import PromptTemplateRegistry


def participant(endpoint_id: str, model_id: str) -> ParticipantProfile:
    return ParticipantProfile(
        id="analyst",
        name="analyst",
        display_name="Analyst",
        participant_type=ParticipantType.panelist,
        model_endpoint_id=endpoint_id,
        model_id=model_id,
        system_prompt_template="panelist_v1",
        perspective="test the tradeoffs carefully",
        expertise="software delivery",
        speaking_style="concise",
        sampling_settings=SamplingSettings(temperature=0.2, top_p=0.9, max_tokens=200),
    )


def context() -> TurnContext:
    return TurnContext(
        central_question="How will AI change software development?",
        phase="main_discussion",
        latest_host_instruction="Respond to the last claim.",
        public_transcript=["host: What matters most?"],
        remaining_seconds=120,
        private_memory=ParticipantMemory(participant_id="analyst"),
        required_dimensions=["productivity", "quality"],
        evidence_summary=[],
        available_evidence_refs=[],
        tool_results=[],
    )


def structured_payload() -> dict:
    return {
        "spoken_text": "The useful answer depends on where review and testing stay rigorous.",
        "intent": "rebuttal",
        "responding_to": None,
        "claims": [
            {
                "text": "AI adoption needs review and testing discipline.",
                "claim_type": "opinion",
                "confidence": 0.75,
                "evidence_refs": [],
            }
        ],
        "questions_for_others": [],
        "requested_follow_up": False,
        "private_memory_update": {
            "unspoken_points": ["Ask about test quality."],
            "open_questions": [],
            "position_summary": "Disciplined adoption matters.",
        },
    }


def iter_object_schemas(schema: dict) -> list[dict]:
    objects: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object" or "properties" in value:
                objects.append(value)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    return objects


def assert_observability_metadata(
    metadata: dict,
    *,
    request_id: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
) -> None:
    assert metadata["model_latency_ms"] >= 0
    assert metadata["adapter_latency_ms"] >= 0
    if request_id is not None:
        assert metadata["adapter_request_id"] == request_id
    assert metadata["token_usage_available"] is (total_tokens is not None)
    assert metadata["token_usage"]["prompt_tokens"] == prompt_tokens
    assert metadata["token_usage"]["completion_tokens"] == completion_tokens
    assert metadata["token_usage"]["total_tokens"] == total_tokens


def test_prompt_template_registry_renders_versioned_discussion_prompt() -> None:
    rendered = PromptTemplateRegistry().render(participant("mock-model", "mock"), context())

    assert rendered.template.id == "panelist_v1"
    assert rendered.template.version == "1.0.0"
    assert rendered.template.created_by == "dialecticore"
    assert rendered.template.change_summary
    assert rendered.messages[0]["role"] == "system"
    assert "Analyst" in rendered.messages[0]["content"]
    assert "Public transcript:\nhost: What matters most?" in rendered.messages[1]["content"]
    assert "Private memory" in rendered.messages[1]["content"]


@pytest.mark.asyncio
async def test_openai_compatible_adapter_posts_structured_chat_completion(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_TOKEN", "test-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        seen["http_referer"] = request.headers.get("http-referer")
        seen["x_title"] = request.headers.get("x-title")
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "accessToken": "leaked-model-token",
                "nested": {"clientSecret": "leaked-model-secret"},
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(structured_payload()),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openai-compatible",
        name="OpenAI Compatible",
        provider_type=ProviderType.openai_compatible,
        base_url="https://provider.example/v1",
        credential_reference="env:OPENAI_COMPATIBLE_TOKEN",
        capabilities={
            "site_url": "https://dialecticore.example",
            "app_title": "DialectiCore",
        },
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "gpt-test"),
        context(),
    )

    assert seen["path"] == "/v1/chat/completions"
    assert seen["authorization"] == "Bearer test-token"
    assert seen["http_referer"] == "https://dialecticore.example"
    assert seen["x_title"] == "DialectiCore"
    assert seen["body"]["model"] == "gpt-test"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert "reasoning" not in seen["body"]
    assert "Public transcript:\nhost: What matters most?" in seen["body"]["messages"][1]["content"]
    assert response.structured.spoken_text.startswith("The useful answer")
    assert response.metadata["prompt_template_id"] == "panelist_v1"
    assert response.metadata["prompt_template_version"] == "1.0.0"
    assert response.metadata["adapter_request_path"] == "/chat/completions"
    assert response.raw["accessToken"] == "[redacted]"
    assert response.raw["nested"]["clientSecret"] == "[redacted]"
    raw_json = json.dumps(response.raw, sort_keys=True)
    assert "leaked-model-token" not in raw_json
    assert "leaked-model-secret" not in raw_json
    assert_observability_metadata(
        response.metadata,
        request_id="chatcmpl-test",
        prompt_tokens=10,
        completion_tokens=7,
        total_tokens=17,
    )


@pytest.mark.asyncio
async def test_openrouter_adapter_omits_numeric_bounds_from_json_schema(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_TOKEN", "test-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["body"] = body
        seen["schema"] = body["response_format"]["json_schema"]["schema"]
        seen["max_tokens"] = body["max_tokens"]
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-openrouter",
                "choices": [{"message": {"content": json.dumps(structured_payload())}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openrouter",
        name="OpenRouter",
        provider_type=ProviderType.openai_compatible,
        base_url="https://openrouter.ai/api/v1",
        credential_reference="env:OPENROUTER_TOKEN",
        capabilities={"provider": "openrouter"},
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "anthropic/claude-sonnet-5"),
        context(),
    )

    schema_json = json.dumps(seen["schema"], sort_keys=True)
    assert '"minimum"' not in schema_json
    assert '"maximum"' not in schema_json
    assert '"default"' not in schema_json
    assert '"minLength"' not in schema_json
    assert '"properties"' in schema_json
    for object_schema in iter_object_schemas(seen["schema"]):
        assert object_schema["additionalProperties"] is False
        properties = object_schema.get("properties")
        if properties:
            assert object_schema["required"] == list(properties)
    question_schema = seen["schema"]["properties"]["questions_for_others"]["items"]
    assert question_schema["properties"] == {
        "participant_id": {"type": "string"},
        "question": {"type": "string"},
    }
    assert seen["max_tokens"] == 1400
    assert seen["body"]["reasoning"] == {"exclude": True}
    assert response.structured.spoken_text.startswith("The useful answer")


@pytest.mark.asyncio
async def test_gateway_retries_malformed_structured_output_with_correction_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_TOKEN", "test-token")
    seen_messages: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_messages.append(body["messages"])
        if len(seen_messages) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-bad",
                    "choices": [{"message": {"content": "not-json"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-good",
                "choices": [{"message": {"content": json.dumps(structured_payload())}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 7, "total_tokens": 15},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openai-compatible",
        name="OpenAI Compatible",
        provider_type=ProviderType.openai_compatible,
        base_url="https://provider.example/v1",
        credential_reference="env:OPENAI_COMPATIBLE_TOKEN",
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "gpt-test"),
        context(),
    )

    assert len(seen_messages) == 2
    assert "Correction required" not in seen_messages[0][1]["content"]
    assert "Correction required" in seen_messages[1][1]["content"]
    assert "StructuredTurnOutput" in seen_messages[1][1]["content"]
    assert response.structured.spoken_text.startswith("The useful answer")
    retry = response.metadata["structured_output_retry"]
    assert retry["schema_version"] == "structured_output_retry.v1"
    assert retry["policy"] == "retry_once_with_correction_prompt"
    assert retry["attempt_count"] == 2
    assert retry["correction_prompt_applied"] is True
    assert "valid structured JSON" in retry["initial_error"]
    assert response.metadata["adapter_request_id"] == "chatcmpl-good"


@pytest.mark.asyncio
async def test_openai_compatible_adapter_reports_safe_parse_failure_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-truncated",
                "provider": "OpenAI",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "max_output_tokens",
                        "message": {"content": "{\"spoken_text\":\"unfinished"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openai-compatible",
        name="OpenAI Compatible",
        provider_type=ProviderType.openai_compatible,
        base_url="https://provider.example/v1",
        credential_reference="env:OPENAI_COMPATIBLE_TOKEN",
    )

    with pytest.raises(StructuredTurnOutputError) as exc_info:
        await gateway.generate_turn(
            endpoint,
            participant(endpoint.id, "gpt-test"),
            context(),
        )

    message = str(exc_info.value)
    assert "provider_context=participant_id=analyst" in message
    assert "model_id=gpt-test" in message
    assert "provider=OpenAI" in message
    assert "finish_reason=length" in message
    assert "native_finish_reason=max_output_tokens" in message
    assert "content_present=True" in message


@pytest.mark.asyncio
async def test_openai_compatible_adapter_reports_provider_timeout(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow provider", request=request)

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="openai-compatible",
        name="OpenAI Compatible",
        provider_type=ProviderType.openai_compatible,
        base_url="https://provider.example/v1",
        credential_reference="env:OPENAI_COMPATIBLE_TOKEN",
        default_timeout_seconds=123,
    )

    with pytest.raises(ValueError) as exc_info:
        await gateway.generate_turn(
            endpoint,
            participant(endpoint.id, "gpt-test"),
            context(),
        )

    message = str(exc_info.value)
    assert "provider request timed out" in message
    assert "participant_id=analyst" in message
    assert "model_id=gpt-test" in message
    assert "timeout_seconds=123" in message


@pytest.mark.asyncio
async def test_gateway_uses_provider_backed_prompt_template_version() -> None:
    templates = [
        DiscussionPromptTemplate(
            id="panelist_v1",
            version="2.1.0",
            participant_type=ParticipantType.panelist,
            system="You are {display_name}. Return only JSON.",
            user="Custom prompt marker for {central_question}: {public_transcript}",
            variables={"turn_context": ["central_question", "public_transcript"]},
            created_by="producer",
            change_summary="Repository override for panelist prompts.",
        )
    ]
    gateway = ModelGateway(prompt_template_provider=lambda: templates)
    endpoint = ModelEndpoint(
        id="mock",
        name="Mock",
        provider_type=ProviderType.mock,
        health_status="healthy",
    )

    response = await gateway.generate_turn(endpoint, participant(endpoint.id, "mock"), context())

    assert response.metadata["prompt_template_id"] == "panelist_v1"
    assert response.metadata["prompt_template_version"] == "2.1.0"
    assert response.metadata["prompt_template_created_by"] == "producer"
    assert response.metadata["prompt_template_change_summary"] == (
        "Repository override for panelist prompts."
    )
    assert_observability_metadata(
        response.metadata,
        request_id=response.metadata["adapter_request_id"],
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )


@pytest.mark.asyncio
async def test_ollama_adapter_posts_chat_with_json_schema() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": json.dumps(structured_payload())},
                "done": True,
                "prompt_eval_count": 9,
                "eval_count": 8,
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="ollama-local",
        name="Ollama Local",
        provider_type=ProviderType.ollama,
        base_url="http://ollama:11434",
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "llama3.1"),
        context(),
    )

    assert seen["path"] == "/api/chat"
    assert seen["body"]["model"] == "llama3.1"
    assert seen["body"]["stream"] is False
    assert "properties" in seen["body"]["format"]
    assert response.structured.claims[0].text == "AI adoption needs review and testing discipline."
    assert response.metadata["prompt_template_id"] == "panelist_v1"
    assert response.metadata["prompt_template_version"] == "1.0.0"
    assert response.metadata["adapter_request_path"] == "/api/chat"
    assert_observability_metadata(
        response.metadata,
        request_id=None,
        prompt_tokens=9,
        completion_tokens=8,
        total_tokens=17,
    )


@pytest.mark.asyncio
async def test_anthropic_compatible_adapter_posts_messages(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_COMPATIBLE_TOKEN", "anthropic-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["anthropic_version"] = request.headers.get("anthropic-version")
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "msg-test",
                "content": [{"type": "text", "text": json.dumps(structured_payload())}],
                "usage": {"input_tokens": 11, "output_tokens": 6},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="anthropic-compatible",
        name="Anthropic Compatible",
        provider_type=ProviderType.anthropic_compatible,
        base_url="https://api.anthropic.example/v1",
        credential_reference="env:ANTHROPIC_COMPATIBLE_TOKEN",
        capabilities={"anthropic_version": "2023-06-01"},
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "claude-test"),
        context(),
    )

    assert seen["path"] == "/v1/messages"
    assert seen["x_api_key"] == "anthropic-token"
    assert seen["anthropic_version"] == "2023-06-01"
    assert seen["body"]["model"] == "claude-test"
    assert seen["body"]["messages"][0]["role"] == "user"
    assert "Return only JSON" in seen["body"]["system"]
    assert response.structured.spoken_text.startswith("The useful answer")
    assert response.metadata["prompt_template_id"] == "panelist_v1"
    assert response.metadata["prompt_template_version"] == "1.0.0"
    assert response.metadata["adapter_request_path"] == "/messages"
    assert_observability_metadata(
        response.metadata,
        request_id="msg-test",
        prompt_tokens=11,
        completion_tokens=6,
        total_tokens=17,
    )


@pytest.mark.asyncio
async def test_mistral_compatible_adapter_posts_chat_completion(monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_COMPATIBLE_TOKEN", "mistral-token")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "id": "mistral-test",
                "choices": [{"message": {"content": json.dumps(structured_payload())}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="mistral-compatible",
        name="Mistral Compatible",
        provider_type=ProviderType.mistral_compatible,
        base_url="https://api.mistral.example/v1",
        credential_reference="env:MISTRAL_COMPATIBLE_TOKEN",
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "mistral-test"),
        context(),
    )

    assert seen["path"] == "/v1/chat/completions"
    assert seen["authorization"] == "Bearer mistral-token"
    assert seen["body"]["model"] == "mistral-test"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert response.structured.claims[0].confidence == 0.75
    assert response.metadata["prompt_template_id"] == "panelist_v1"
    assert response.metadata["prompt_template_version"] == "1.0.0"
    assert response.metadata["adapter_request_path"] == "/chat/completions"
    assert_observability_metadata(
        response.metadata,
        request_id="mistral-test",
        prompt_tokens=12,
        completion_tokens=5,
        total_tokens=17,
    )


@pytest.mark.asyncio
async def test_generic_http_adapter_posts_configured_turn_request(monkeypatch) -> None:
    monkeypatch.setenv("GENERIC_MODEL_TOKEN", "Token generic")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "request_id": "generic-test",
                "data": {"turn": structured_payload()},
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="generic-http",
        name="Generic HTTP",
        provider_type=ProviderType.generic_http,
        base_url="https://models.example/internal",
        credential_reference="env:GENERIC_MODEL_TOKEN",
        capabilities={
            "request_path": "turns/generate",
            "authorization_scheme": "raw",
            "response_json_path": "data.turn",
        },
    )

    response = await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "custom-panelist"),
        context(),
    )

    assert seen["path"] == "/internal/turns/generate"
    assert seen["authorization"] == "Token generic"
    assert seen["body"]["schema_version"] == "dialecticore.generic_model_turn_request.v1"
    assert seen["body"]["model"] == "custom-panelist"
    assert seen["body"]["participant"]["id"] == "analyst"
    assert seen["body"]["context"]["private_memory"]["participant_id"] == "analyst"
    assert "spoken_text" in seen["body"]["turn_schema"]["properties"]
    assert response.structured.private_memory_update.position_summary == (
        "Disciplined adoption matters."
    )
    assert response.metadata["prompt_template_id"] == "panelist_v1"
    assert response.metadata["prompt_template_version"] == "1.0.0"
    assert response.metadata["adapter_request_path"] == "/turns/generate"
    assert_observability_metadata(
        response.metadata,
        request_id="generic-test",
        prompt_tokens=5,
        completion_tokens=4,
        total_tokens=9,
    )


@pytest.mark.asyncio
async def test_generic_http_adapter_includes_permitted_tool_results() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["context"] = body["context"]
        return httpx.Response(200, json={"output": structured_payload()})

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(
        id="generic",
        name="Generic",
        provider_type=ProviderType.generic_http,
        base_url="https://provider.example",
    )
    tool_results = [
        {
            "tool_name": "evidence_pack_lookup",
            "claim_text": "Source-backed claim",
            "evidence_refs": ["source-a"],
        }
    ]

    await gateway.generate_turn(
        endpoint,
        participant(endpoint.id, "model-x"),
        replace(context(), tool_results=tool_results),
    )

    assert seen["context"]["tool_results"] == tool_results
