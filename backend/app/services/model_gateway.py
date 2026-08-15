from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
from app.core.credentials import normalize_credential_reference
from app.domain.enums import ProviderType
from app.domain.schemas import (
    ModelEndpoint,
    ParticipantMemory,
    ParticipantProfile,
    StructuredTurnOutput,
)
from app.services.prompt_templates import PromptTemplateRegistry
from app.services.redaction import safe_provider_response_payload
from pydantic import ValidationError


@dataclass(frozen=True)
class TurnContext:
    central_question: str
    phase: str
    latest_host_instruction: str
    public_transcript: list[str]
    remaining_seconds: float
    private_memory: ParticipantMemory
    required_dimensions: list[str]
    evidence_summary: list[str]
    available_evidence_refs: list[str]
    tool_results: list[dict[str, Any]]
    discussion_intensity: str = "medium"


@dataclass(frozen=True)
class ModelResponse:
    structured: StructuredTurnOutput
    raw: dict
    metadata: dict


class ModelClient(Protocol):
    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        """Generate exactly one participant turn."""


class SecretResolver:
    docker_secret_root = Path("/run/secrets")

    def resolve(self, credential_reference: str | None) -> str | None:
        if credential_reference is None:
            return None
        try:
            credential_reference = normalize_credential_reference(credential_reference)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if credential_reference is None:
            return None
        if credential_reference.startswith("env:"):
            env_name = credential_reference.removeprefix("env:")
            value = os.getenv(env_name)
            if value:
                return value
            file_value = os.getenv(f"{env_name}_FILE")
            if file_value:
                path = Path(file_value)
                if not path.is_absolute():
                    raise RuntimeError("env credential file reference must use an absolute path")
                return self._read_secret_file(path, credential_reference)
            else:
                raise RuntimeError("credential reference is not available")
        if credential_reference.startswith("file:"):
            path = Path(credential_reference.removeprefix("file:"))
            if not path.is_absolute():
                raise RuntimeError("file credential reference must use an absolute path")
            return self._read_secret_file(path, credential_reference)
        if credential_reference.startswith("docker-secret:"):
            secret_name = credential_reference.removeprefix("docker-secret:")
            if (
                not secret_name
                or "/" in secret_name
                or "\\" in secret_name
                or secret_name in {".", ".."}
            ):
                raise RuntimeError("docker-secret credential reference is invalid")
            return self._read_secret_file(
                self.docker_secret_root / secret_name,
                credential_reference,
            )
        raise RuntimeError("unsupported credential reference scheme")

    def _read_secret_file(self, path: Path, credential_reference: str) -> str:
        try:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError("credential reference is not available") from exc
        if not value:
            raise RuntimeError("credential reference is not available")
        return value


class StructuredTurnOutputError(ValueError):
    """Raised when a provider response cannot be parsed as StructuredTurnOutput."""


def parse_structured_turn_output(payload: str | dict) -> StructuredTurnOutput:
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StructuredTurnOutputError(
                "provider response did not contain valid structured JSON"
            ) from exc
    else:
        decoded = payload
    try:
        return StructuredTurnOutput.model_validate(decoded)
    except ValidationError as exc:
        raise StructuredTurnOutputError(_structured_validation_message(exc)) from exc


def _structured_validation_message(exc: ValidationError) -> str:
    issues = []
    for issue in exc.errors(include_input=False)[:6]:
        location = ".".join(str(part) for part in issue.get("loc", ())) or "root"
        message = str(issue.get("msg") or "invalid value")
        issues.append(f"{location}: {message}")
    if not issues:
        return "provider response did not match StructuredTurnOutput"
    return "provider response did not match StructuredTurnOutput: " + "; ".join(issues)


def turn_schema() -> dict:
    return StructuredTurnOutput.model_json_schema()


def provider_turn_schema(endpoint: ModelEndpoint) -> dict:
    schema = json.loads(json.dumps(turn_schema()))
    if endpoint.capabilities.get("provider") == "openrouter":
        schema = _strip_unsupported_json_schema_keywords(
            schema,
            unsupported={"default", "maximum", "minimum", "maxLength", "minLength"},
        )
        _specialize_openrouter_question_schema(schema)
        return _make_openrouter_schema_strict(schema)
    return schema


def provider_max_tokens(endpoint: ModelEndpoint, participant: ParticipantProfile) -> int:
    configured = participant.sampling_settings.max_tokens
    if endpoint.capabilities.get("provider") == "openrouter":
        minimum = int(endpoint.capabilities.get("minimum_structured_max_tokens") or 1400)
        return max(configured, minimum)
    return configured


def openrouter_reasoning_parameters(endpoint: ModelEndpoint) -> dict[str, Any]:
    if endpoint.capabilities.get("provider") != "openrouter":
        return {}
    return {"reasoning": {"exclude": True}}


def _strip_unsupported_json_schema_keywords(value: Any, *, unsupported: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_unsupported_json_schema_keywords(item, unsupported=unsupported)
            for key, item in value.items()
            if key not in unsupported
        }
    if isinstance(value, list):
        return [
            _strip_unsupported_json_schema_keywords(item, unsupported=unsupported)
            for item in value
        ]
    return value


def _specialize_openrouter_question_schema(schema: dict[str, Any]) -> None:
    questions = schema.get("properties", {}).get("questions_for_others")
    if not isinstance(questions, dict):
        return
    questions["items"] = {
        "type": "object",
        "properties": {
            "participant_id": {"type": "string"},
            "question": {"type": "string"},
        },
        "required": ["participant_id", "question"],
        "additionalProperties": False,
    }


def _make_openrouter_schema_strict(value: Any) -> Any:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            value[key] = _make_openrouter_schema_strict(item)
        if value.get("type") == "object" or "properties" in value:
            value["additionalProperties"] = False
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _make_openrouter_schema_strict(item)
    return value


prompt_templates = PromptTemplateRegistry()


def build_prompt(
    participant: ParticipantProfile,
    context: TurnContext,
    registry: PromptTemplateRegistry | None = None,
) -> list[dict[str, str]]:
    return (registry or prompt_templates).render(participant, context).messages


def prompt_template_metadata(
    participant: ParticipantProfile,
    context: TurnContext,
    registry: PromptTemplateRegistry | None = None,
) -> dict[str, Any]:
    return (registry or prompt_templates).render(participant, context).metadata()


def auth_headers(
    endpoint: ModelEndpoint,
    secret_resolver: SecretResolver,
    default_scheme: str = "bearer",
) -> dict[str, str]:
    token = secret_resolver.resolve(endpoint.credential_reference)
    headers = {"content-type": "application/json"}
    scheme = str(endpoint.capabilities.get("authorization_scheme") or default_scheme).lower()
    if not token:
        return headers
    if scheme == "bearer":
        headers["authorization"] = f"Bearer {token}"
    elif scheme == "api_key":
        headers["x-api-key"] = token
    elif scheme == "raw":
        headers["authorization"] = token
    else:
        raise ValueError(f"unsupported authorization scheme {scheme}")
    site_url = endpoint.capabilities.get("site_url")
    if isinstance(site_url, str) and site_url:
        headers["http-referer"] = site_url
    app_title = endpoint.capabilities.get("app_title")
    if isinstance(app_title, str) and app_title:
        headers["x-title"] = app_title
    return headers


def capability_path(endpoint: ModelEndpoint, key: str, default: str) -> str:
    raw_path = endpoint.capabilities.get(key, default)
    path = str(raw_path or default)
    return path if path.startswith("/") else f"/{path}"


def extract_json_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise StructuredTurnOutputError(
                    f"response path {path} did not match provider response"
                ) from exc
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise StructuredTurnOutputError(
                f"response path {path} did not match provider response"
            )
    return current


def extract_structured_payload(raw: dict, endpoint: ModelEndpoint) -> str | dict:
    configured_path = endpoint.capabilities.get("response_json_path")
    if configured_path:
        return extract_json_path(raw, str(configured_path))
    if isinstance(raw.get("structured"), str | dict):
        return raw["structured"]
    if isinstance(raw.get("output"), str | dict):
        return raw["output"]
    if isinstance(raw.get("content"), str | dict):
        return raw["content"]
    if isinstance(raw.get("response"), str | dict):
        return raw["response"]
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str | dict):
            return message["content"]
    content_blocks = raw.get("content")
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    raise StructuredTurnOutputError("provider response did not contain structured turn output")


def _monotonic_ms() -> float:
    return time.perf_counter() * 1000


def _elapsed_ms(start_ms: float) -> float:
    return round(max(_monotonic_ms() - start_ms, 0.0), 3)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _int_or_none(payload.get(key))
        if value is not None:
            return value
    return None


def _adapter_request_id(raw: dict[str, Any]) -> str | None:
    for key in ("id", "request_id", "response_id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    response = raw.get("response")
    if isinstance(response, dict):
        value = response.get("id") or response.get("request_id")
        if isinstance(value, str) and value:
            return value
    return None


def token_usage_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    usage = raw.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = _first_int(usage, "prompt_tokens", "input_tokens")
        completion_tokens = _first_int(usage, "completion_tokens", "output_tokens")
        total_tokens = _first_int(usage, "total_tokens")
        if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        available = any(
            value is not None for value in (prompt_tokens, completion_tokens, total_tokens)
        )
        return {
            "token_usage_available": available,
            "token_usage": {
                "source": "provider_usage" if available else "unavailable",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        }

    prompt_tokens = _first_int(raw, "prompt_eval_count", "input_tokens", "prompt_tokens")
    completion_tokens = _first_int(raw, "eval_count", "output_tokens", "completion_tokens")
    total_tokens = _first_int(raw, "total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    available = any(value is not None for value in (prompt_tokens, completion_tokens, total_tokens))
    return {
        "token_usage_available": available,
        "token_usage": {
            "source": "provider_counts" if available else "unavailable",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


def generation_observability_metadata(raw: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    metadata = {
        "model_latency_ms": latency_ms,
        "adapter_latency_ms": latency_ms,
        **token_usage_metadata(raw),
    }
    request_id = _adapter_request_id(raw)
    if request_id is not None:
        metadata["adapter_request_id"] = request_id
    return metadata


class MockModelClient:
    def __init__(self, registry: PromptTemplateRegistry | None = None) -> None:
        self.registry = registry or prompt_templates

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        start_ms = _monotonic_ms()
        sequence_hint = len(context.public_transcript) + 1
        dimension = (
            context.required_dimensions[(sequence_hint - 1) % len(context.required_dimensions)]
            if context.required_dimensions
            else "the central question"
        )
        if participant.participant_type == "host":
            spoken = self._host_text(participant, context, sequence_hint, dimension)
            intent = "question" if sequence_hint > 1 else "opening"
            requested_follow_up = True
        else:
            spoken = self._panelist_text(participant, context, dimension)
            intent = "rebuttal" if context.public_transcript else "opening_position"
            requested_follow_up = False

        structured = StructuredTurnOutput(
            spoken_text=spoken,
            intent=intent,
            responding_to=None,
            claims=[
                {
                    "text": (
                        f"{participant.display_name} addresses {dimension} "
                        "for the episode topic."
                    ),
                    "claim_type": "supported" if context.available_evidence_refs else "opinion",
                    "confidence": 0.64,
                    "evidence_refs": context.available_evidence_refs[:1],
                }
            ],
            questions_for_others=[],
            requested_follow_up=requested_follow_up,
            private_memory_update={
                "unspoken_points": [f"Return to {dimension} if time allows."],
                "open_questions": [],
                "position_summary": participant.perspective,
            },
        )
        raw = {
            "provider": endpoint.provider_type,
            "model": participant.model_id,
            "request_id": f"mock-{uuid4()}",
            "content": structured.model_dump(),
        }
        return ModelResponse(
            structured=structured,
            raw=raw,
            metadata={
                "provider_type": endpoint.provider_type,
                "model_endpoint_id": endpoint.id,
                "model_id": participant.model_id,
                "prompt_template": participant.system_prompt_template,
                **prompt_template_metadata(participant, context, self.registry),
                "sampling": participant.sampling_settings.model_dump(),
                **generation_observability_metadata(raw, _elapsed_ms(start_ms)),
            },
        )

    def _host_text(
        self,
        participant: ParticipantProfile,
        context: TurnContext,
        sequence_hint: int,
        dimension: str,
    ) -> str:
        if sequence_hint == 1:
            return (
                f"Welcome. Today we are examining: {context.central_question}. "
                f"I want the panel to start with concrete views on {dimension}, and I will keep us "
                "focused on disagreements, evidence, and tradeoffs."
            )
        if context.remaining_seconds < 50:
            return (
                "We are near the end, so I want concise closing positions. "
                "Name what you believe changed in the discussion and what remains uncertain."
            )
        return (
            f"Let us test the last point against {dimension}. "
            "Please respond directly to what was just said, and separate facts from judgment."
        )

    def _panelist_text(
        self,
        participant: ParticipantProfile,
        context: TurnContext,
        dimension: str,
    ) -> str:
        prior = "the moderator's question"
        if context.public_transcript:
            prior = "the previous answer"
        return (
            f"From my perspective, {participant.perspective}. In response to {prior}, "
            f"the important issue is {dimension}. A useful answer should connect that point "
            "to actual production constraints instead of treating the topic as a slogan."
        )


class OpenAICompatibleClient:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: PromptTemplateRegistry | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport
        self.registry = registry or prompt_templates

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        if not endpoint.base_url:
            raise ValueError("OpenAI-compatible endpoint requires base_url")
        headers = auth_headers(endpoint, self.secret_resolver)
        payload = {
            "model": participant.model_id,
            "messages": build_prompt(participant, context, self.registry),
            "temperature": participant.sampling_settings.temperature,
            "top_p": participant.sampling_settings.top_p,
            "max_tokens": provider_max_tokens(endpoint, participant),
            **openrouter_reasoning_parameters(endpoint),
            "response_format": {
                "type": "json_schema",
                    "json_schema": {
                        "name": "structured_turn_output",
                        "schema": provider_turn_schema(endpoint),
                        "strict": True,
                    },
                },
        }
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
        ) as client:
            start_ms = _monotonic_ms()
            try:
                response = await client.post("/chat/completions", json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise ValueError(
                    "provider request timed out; "
                    f"provider_context=participant_id={participant.id}, "
                    f"model_id={participant.model_id}, "
                    f"timeout_seconds={endpoint.default_timeout_seconds}"
                ) from exc
            response.raise_for_status()
            latency_ms = _elapsed_ms(start_ms)
        raw = response.json()
        content = raw["choices"][0]["message"]["content"]
        try:
            structured = parse_structured_turn_output(content)
        except StructuredTurnOutputError as exc:
            raise StructuredTurnOutputError(
                f"{exc}; {openai_compatible_error_context(raw, participant)}"
            ) from exc
        return ModelResponse(
            structured=structured,
            raw=raw,
            metadata={
                "provider_type": endpoint.provider_type,
                "model_endpoint_id": endpoint.id,
                "model_id": participant.model_id,
                "prompt_template": participant.system_prompt_template,
                **prompt_template_metadata(participant, context, self.registry),
                "sampling": participant.sampling_settings.model_dump(),
                "adapter_request_path": "/chat/completions",
                **generation_observability_metadata(raw, latency_ms),
            },
        )


def openai_compatible_error_context(raw: dict, participant: ParticipantProfile) -> str:
    choice = None
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else None
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    content_length = len(content) if isinstance(content, str) else 0
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    native_finish_reason = choice.get("native_finish_reason") if isinstance(choice, dict) else None
    provider = raw.get("provider")
    return (
        "provider_context="
        f"participant_id={participant.id}, "
        f"model_id={participant.model_id}, "
        f"provider={provider}, "
        f"finish_reason={finish_reason}, "
        f"native_finish_reason={native_finish_reason}, "
        f"content_present={isinstance(content, str)}, "
        f"content_length={content_length}"
    )


class OllamaClient:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: PromptTemplateRegistry | None = None,
    ) -> None:
        self.transport = transport
        self.registry = registry or prompt_templates

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        if not endpoint.base_url:
            raise ValueError("Ollama endpoint requires base_url")
        payload = {
            "model": participant.model_id,
            "messages": build_prompt(participant, context, self.registry),
            "stream": False,
            "format": turn_schema(),
            "options": {
                "temperature": participant.sampling_settings.temperature,
                "top_p": participant.sampling_settings.top_p,
                "num_predict": participant.sampling_settings.max_tokens,
            },
        }
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
        ) as client:
            start_ms = _monotonic_ms()
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
            latency_ms = _elapsed_ms(start_ms)
        raw = response.json()
        content = raw["message"]["content"]
        structured = parse_structured_turn_output(content)
        return ModelResponse(
            structured=structured,
            raw=raw,
            metadata={
                "provider_type": endpoint.provider_type,
                "model_endpoint_id": endpoint.id,
                "model_id": participant.model_id,
                "prompt_template": participant.system_prompt_template,
                **prompt_template_metadata(participant, context, self.registry),
                "sampling": participant.sampling_settings.model_dump(),
                "adapter_request_path": "/api/chat",
                **generation_observability_metadata(raw, latency_ms),
            },
        )


class AnthropicCompatibleClient:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: PromptTemplateRegistry | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport
        self.registry = registry or prompt_templates

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        if not endpoint.base_url:
            raise ValueError("Anthropic-compatible endpoint requires base_url")
        prompt = build_prompt(participant, context, self.registry)
        system_prompt = "\n\n".join(
            message["content"] for message in prompt if message["role"] == "system"
        )
        user_messages = [
            {"role": message["role"], "content": message["content"]}
            for message in prompt
            if message["role"] != "system"
        ]
        headers = auth_headers(endpoint, self.secret_resolver, default_scheme="api_key")
        headers["anthropic-version"] = str(
            endpoint.capabilities.get("anthropic_version") or "2023-06-01"
        )
        payload = {
            "model": participant.model_id,
            "system": system_prompt,
            "messages": user_messages,
            "temperature": participant.sampling_settings.temperature,
            "top_p": participant.sampling_settings.top_p,
            "max_tokens": participant.sampling_settings.max_tokens,
        }
        request_path = capability_path(endpoint, "request_path", "/messages")
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
        ) as client:
            start_ms = _monotonic_ms()
            response = await client.post(request_path, json=payload, headers=headers)
            response.raise_for_status()
            latency_ms = _elapsed_ms(start_ms)
        raw = response.json()
        structured = parse_structured_turn_output(extract_structured_payload(raw, endpoint))
        return ModelResponse(
            structured=structured,
            raw=raw,
            metadata={
                "provider_type": endpoint.provider_type,
                "model_endpoint_id": endpoint.id,
                "model_id": participant.model_id,
                "prompt_template": participant.system_prompt_template,
                **prompt_template_metadata(participant, context, self.registry),
                "sampling": participant.sampling_settings.model_dump(),
                "adapter_request_path": request_path,
                **generation_observability_metadata(raw, latency_ms),
            },
        )


class MistralCompatibleClient:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: PromptTemplateRegistry | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport
        self.registry = registry or prompt_templates

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        if not endpoint.base_url:
            raise ValueError("Mistral-compatible endpoint requires base_url")
        headers = auth_headers(endpoint, self.secret_resolver)
        payload = {
            "model": participant.model_id,
            "messages": build_prompt(participant, context, self.registry),
            "temperature": participant.sampling_settings.temperature,
            "top_p": participant.sampling_settings.top_p,
            "max_tokens": participant.sampling_settings.max_tokens,
            "response_format": {"type": "json_object"},
        }
        request_path = capability_path(endpoint, "request_path", "/chat/completions")
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
        ) as client:
            start_ms = _monotonic_ms()
            response = await client.post(request_path, json=payload, headers=headers)
            response.raise_for_status()
            latency_ms = _elapsed_ms(start_ms)
        raw = response.json()
        structured = parse_structured_turn_output(extract_structured_payload(raw, endpoint))
        return ModelResponse(
            structured=structured,
            raw=raw,
            metadata={
                "provider_type": endpoint.provider_type,
                "model_endpoint_id": endpoint.id,
                "model_id": participant.model_id,
                "prompt_template": participant.system_prompt_template,
                **prompt_template_metadata(participant, context, self.registry),
                "sampling": participant.sampling_settings.model_dump(),
                "adapter_request_path": request_path,
                **generation_observability_metadata(raw, latency_ms),
            },
        )


class GenericHttpClient:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: PromptTemplateRegistry | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport
        self.registry = registry or prompt_templates

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        if not endpoint.base_url:
            raise ValueError("Generic HTTP endpoint requires base_url")
        headers = auth_headers(endpoint, self.secret_resolver)
        request_path = capability_path(endpoint, "request_path", "/generate-turn")
        method = str(endpoint.capabilities.get("request_method") or "POST").upper()
        if method != "POST":
            raise ValueError("generic HTTP model adapter currently supports POST requests only")
        payload = {
            "schema_version": "dialecticore.generic_model_turn_request.v1",
            "model": participant.model_id,
            "participant": participant.model_dump(mode="json"),
            "messages": build_prompt(participant, context, self.registry),
            "turn_schema": turn_schema(),
            "sampling": participant.sampling_settings.model_dump(mode="json"),
            "context": {
                "central_question": context.central_question,
                "phase": context.phase,
                "latest_host_instruction": context.latest_host_instruction,
                "public_transcript": context.public_transcript,
                "remaining_seconds": context.remaining_seconds,
                "required_dimensions": context.required_dimensions,
                "evidence_summary": context.evidence_summary,
                "available_evidence_refs": context.available_evidence_refs,
                "tool_results": context.tool_results,
                "private_memory": context.private_memory.model_dump(mode="json"),
            },
        }
        async with httpx.AsyncClient(
            base_url=endpoint.base_url.rstrip("/"),
            timeout=endpoint.default_timeout_seconds,
            transport=self.transport,
        ) as client:
            start_ms = _monotonic_ms()
            response = await client.post(request_path, json=payload, headers=headers)
            response.raise_for_status()
            latency_ms = _elapsed_ms(start_ms)
        raw = response.json()
        structured = parse_structured_turn_output(extract_structured_payload(raw, endpoint))
        return ModelResponse(
            structured=structured,
            raw=raw,
            metadata={
                "provider_type": endpoint.provider_type,
                "model_endpoint_id": endpoint.id,
                "model_id": participant.model_id,
                "prompt_template": participant.system_prompt_template,
                **prompt_template_metadata(participant, context, self.registry),
                "sampling": participant.sampling_settings.model_dump(),
                "adapter_request_path": request_path,
                **generation_observability_metadata(raw, latency_ms),
            },
        )


class ModelGateway:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        secret_resolver: SecretResolver | None = None,
        prompt_template_provider: Any | None = None,
    ) -> None:
        registry = PromptTemplateRegistry(template_provider=prompt_template_provider)
        self._clients: dict[ProviderType, ModelClient] = {
            ProviderType.mock: MockModelClient(registry),
            ProviderType.openai_compatible: OpenAICompatibleClient(
                secret_resolver,
                transport,
                registry,
            ),
            ProviderType.ollama: OllamaClient(transport, registry),
            ProviderType.anthropic_compatible: AnthropicCompatibleClient(
                secret_resolver,
                transport,
                registry,
            ),
            ProviderType.mistral_compatible: MistralCompatibleClient(
                secret_resolver,
                transport,
                registry,
            ),
            ProviderType.generic_http: GenericHttpClient(secret_resolver, transport, registry),
        }

    async def generate_turn(
        self,
        endpoint: ModelEndpoint,
        participant: ParticipantProfile,
        context: TurnContext,
    ) -> ModelResponse:
        client = self._clients[endpoint.provider_type]
        retry_metadata: dict[str, Any] = {}
        try:
            response = await client.generate_turn(endpoint, participant, context)
        except StructuredTurnOutputError as exc:
            correction_context = correction_turn_context(context, exc)
            response = await client.generate_turn(endpoint, participant, correction_context)
            retry_metadata = {
                "structured_output_retry": {
                    "schema_version": "structured_output_retry.v1",
                    "policy": "retry_once_with_correction_prompt",
                    "attempt_count": 2,
                    "initial_error": str(exc),
                    "correction_prompt_applied": True,
                }
            }
        safe_raw = safe_provider_response_payload(response.raw)
        return ModelResponse(
            structured=response.structured,
            raw=safe_raw if isinstance(safe_raw, dict) else {},
            metadata={**response.metadata, **retry_metadata},
        )


def correction_turn_context(
    context: TurnContext,
    error: StructuredTurnOutputError,
) -> TurnContext:
    correction = (
        "Correction required: the previous provider response could not be accepted as "
        f"StructuredTurnOutput ({error}). Regenerate this same turn now. Return only valid "
        "JSON matching StructuredTurnOutput. Do not add markdown, commentary, hidden "
        "reasoning, or fields outside the schema."
    )
    return replace(
        context,
        latest_host_instruction=f"{context.latest_host_instruction}\n\n{correction}",
    )
