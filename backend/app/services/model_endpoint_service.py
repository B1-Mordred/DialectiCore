from __future__ import annotations

from typing import Any

import httpx
from app.domain.enums import ProviderType
from app.domain.schemas import ModelEndpoint
from app.services.model_gateway import SecretResolver, auth_headers, capability_path
from app.services.redaction import safe_provider_response_payload


class ModelEndpointService:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport

    async def check_endpoint_health(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        if endpoint.provider_type == ProviderType.mock:
            return endpoint.model_copy(
                update={
                    "health_status": "healthy",
                    "capabilities": {
                        **endpoint.capabilities,
                        "turn_generation": True,
                        "structured_turn_output": True,
                        "deterministic": True,
                    },
                }
            )
        if not endpoint.base_url:
            return endpoint.model_copy(update={"health_status": "unconfigured"})

        capabilities = self._base_capabilities(endpoint)
        request_path = capability_path(endpoint, "health_path", self._default_health_path(endpoint))
        headers = auth_headers(
            endpoint,
            self.secret_resolver,
            default_scheme=self._default_authorization_scheme(endpoint),
        )
        headers["accept"] = "application/json"

        try:
            async with httpx.AsyncClient(
                base_url=endpoint.base_url.rstrip("/"),
                timeout=endpoint.default_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(request_path, headers=headers)
        except httpx.HTTPError as exc:
            capabilities["last_health_error"] = str(exc)
            capabilities["health_path"] = request_path
            return endpoint.model_copy(
                update={"health_status": "unhealthy", "capabilities": capabilities}
            )

        health_status = "healthy" if response.is_success else "unhealthy"
        capabilities["health_path"] = request_path
        capabilities["health_status_code"] = response.status_code
        payload = self._response_json(response)
        if isinstance(payload, dict):
            discovered = payload.get("capabilities")
            if isinstance(discovered, dict):
                safe_discovered = safe_provider_response_payload(discovered)
                if isinstance(safe_discovered, dict):
                    capabilities.update(safe_discovered)
            capabilities.update(self._model_listing_capabilities(payload))

        return endpoint.model_copy(
            update={"health_status": health_status, "capabilities": capabilities}
        )

    def _base_capabilities(self, endpoint: ModelEndpoint) -> dict[str, Any]:
        capabilities = {
            **endpoint.capabilities,
            "turn_generation": True,
            "structured_turn_output": True,
        }
        if endpoint.provider_type == ProviderType.openai_compatible:
            capabilities.update({"chat_completions": True, "json_schema_response": True})
        elif endpoint.provider_type == ProviderType.ollama:
            capabilities.update({"chat": True, "json_schema_format": True})
        elif endpoint.provider_type == ProviderType.anthropic_compatible:
            capabilities.update({"messages": True, "text_content_blocks": True})
        elif endpoint.provider_type == ProviderType.mistral_compatible:
            capabilities.update({"chat_completions": True, "json_object_response": True})
        elif endpoint.provider_type == ProviderType.generic_http:
            capabilities.update({"generic_turn_request": True})
        return capabilities

    def _default_health_path(self, endpoint: ModelEndpoint) -> str:
        if endpoint.provider_type == ProviderType.ollama:
            return "/api/tags"
        if endpoint.provider_type in {
            ProviderType.openai_compatible,
            ProviderType.anthropic_compatible,
            ProviderType.mistral_compatible,
        }:
            return "/models"
        return "/health"

    def _default_authorization_scheme(self, endpoint: ModelEndpoint) -> str:
        if endpoint.provider_type == ProviderType.anthropic_compatible:
            return "api_key"
        return "bearer"

    def _response_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    def _model_listing_capabilities(self, payload: dict) -> dict[str, Any]:
        model_items = payload.get("data")
        if not isinstance(model_items, list):
            model_items = payload.get("models")
        if not isinstance(model_items, list):
            return {}
        model_ids = [
            item.get("id") or item.get("name") or item.get("model")
            for item in model_items
            if isinstance(item, dict)
        ]
        model_ids = [str(item) for item in model_ids if item]
        return {
            "model_listing": True,
            "model_count": len(model_items),
            "model_ids": model_ids[:20],
        }
