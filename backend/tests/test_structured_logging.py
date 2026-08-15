from __future__ import annotations

import logging

import pytest
from app.core.config import Settings
from app.services.structured_logging import (
    StructuredJsonFormatter,
    correlation_id_from_headers,
    path_identifiers,
    request_log_payload,
    sanitize_correlation_id,
    setup_structured_logging,
)


def test_path_identifiers_extract_real_episode_route_ids() -> None:
    assert path_identifiers(
        "/api/v1/episodes/episode-1/assets/asset-2/replace"
    ) == {
        "episode_id": "episode-1",
        "asset_id": "asset-2",
    }
    assert path_identifiers(
        "/api/v1/episodes/episode-1/discussion/turns/turn-2/regenerate"
    ) == {
        "episode_id": "episode-1",
        "turn_id": "turn-2",
    }
    assert path_identifiers(
        "/api/v1/episodes/episode-1/approvals/approval-2/decision"
    ) == {
        "episode_id": "episode-1",
        "approval_id": "approval-2",
    }


def test_path_identifiers_extract_workflow_ids_from_comfyui_routes() -> None:
    assert path_identifiers("/api/v1/comfyui-workflows/workflow-1") == {
        "workflow_id": "workflow-1"
    }
    assert path_identifiers("/api/v1/workflows/workflow-2") == {
        "workflow_id": "workflow-2"
    }


def test_path_identifiers_do_not_invent_publish_job_ids_for_list_routes() -> None:
    assert path_identifiers("/api/v1/episodes/episode-1/publish-jobs") == {
        "episode_id": "episode-1"
    }


def test_request_log_payload_includes_route_identifiers() -> None:
    assert request_log_payload(
        method="GET",
        path="/api/v1/comfyui-workflows/workflow-1",
        status_code=200,
        duration_ms=12.3456,
        client_host="127.0.0.1",
    ) == {
        "schema_version": "dialecticore.api_request_log.v1",
        "method": "GET",
        "path": "/api/v1/comfyui-workflows/workflow-1",
        "status_code": 200,
        "duration_ms": 12.346,
        "client_host": "127.0.0.1",
        "workflow_id": "workflow-1",
    }


def test_correlation_id_helpers_sanitize_supplied_request_ids() -> None:
    assert sanitize_correlation_id(" operator trace/1 ") == "operator-trace-1"
    assert correlation_id_from_headers({"x-request-id": "trace/2"}) == "trace-2"


def test_setup_structured_logging_applies_operator_log_level_without_duplicates() -> None:
    logger = logging.getLogger("dialecticore")
    original_level = logger.level
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)
    logger.handlers = []
    try:
        setup_structured_logging(Settings(log_level="debug"))

        structured_handlers = [
            handler
            for handler in logger.handlers
            if getattr(handler, "_dialecticore_structured", False)
        ]
        assert logger.level == logging.DEBUG
        assert logger.propagate is False
        assert len(structured_handlers) == 1
        assert isinstance(structured_handlers[0].formatter, StructuredJsonFormatter)

        setup_structured_logging(Settings(log_level="warning"))

        structured_handlers = [
            handler
            for handler in logger.handlers
            if getattr(handler, "_dialecticore_structured", False)
        ]
        assert logger.level == logging.WARNING
        assert len(structured_handlers) == 1
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_setup_structured_logging_rejects_invalid_operator_log_level() -> None:
    with pytest.raises(RuntimeError, match="DIALECTICORE_LOG_LEVEL must be one of"):
        setup_structured_logging(Settings(log_level="verbose"))
