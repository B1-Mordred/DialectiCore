import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from scripts.live_episode_smoke import (
    append_episode_provider_requirements,
    append_provider_preflight_requirements,
    append_provider_requirements_and_refresh_report,
    approve_pending_gates,
    artifact_download_evidence,
    build_episode_participant_entries,
    cast_readiness_summary,
    cleanup_provider_preflight_draft,
    cleanup_started_workflow,
    cleanup_started_workflow_after_error,
    compact_real_life_test_readiness,
    create_episode,
    discussion_speaker_coverage_summary,
    enable_synthetic_mock_endpoints,
    enable_synthetic_mock_participants,
    enable_synthetic_mock_workflows,
    endpoint_update_payload,
    ensure_workflow_started,
    episode_provider_requirement_issues,
    get_cast_readiness_summary,
    get_live_provider_readiness_summary,
    inspect_delivery_package,
    live_provider_readiness_summary,
    native_visual_preflight_should_stop,
    native_visual_preflight_summary,
    parse_participant_ids,
    participant_update_payload,
    production_acceptance_summary,
    production_test_summary,
    provider_smoke_preflight_summary,
    refresh_comfyui_health,
    refresh_production_test_report_after_handoff,
    restore_synthetic_mock_endpoints,
    restore_synthetic_mock_participants,
    restore_synthetic_mock_workflows,
    run_episode_until_blocked,
    run_until_blocked_summary,
    should_cleanup_provider_preflight_draft,
    should_cleanup_start_only_run,
    should_cleanup_start_only_run_after_error,
    should_wait_for_native_visual_admission,
    smoke_invocation_summary,
    start_only_smoke_succeeded,
    synthetic_endpoint_setup_summary,
    synthetic_participant_setup_summary,
    synthetic_workflow_setup_summary,
    wait_for_native_visual_admission,
    workflow_update_payload,
)


def test_live_episode_smoke_starts_durable_workflow_and_records_replay() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/workflow/start"):
            return httpx.Response(
                200,
                json={
                    "id": "episode-a",
                    "discussion_session": None,
                    "workflow_control": {
                        "run": {
                            "run_id": "run-a",
                            "state": "running",
                            "current_stage": "DRAFT",
                            "started_by": "live-smoke",
                        }
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/workflow/replay"):
            return httpx.Response(
                200,
                json={
                    "status": "pass",
                    "event_count": 1,
                    "event_log_checksum": "sha256:replay",
                    "replayed": {"state": "running", "current_stage": "DRAFT"},
                    "current": {"state": "running", "current_stage": "DRAFT"},
                    "issues": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = ensure_workflow_started(
            client,
            "http://test",
            "episode-a",
            user_id="live-smoke",
        )

    assert requests == [
        ("POST", "/api/v1/episodes/episode-a/workflow/start"),
        ("GET", "/api/v1/episodes/episode-a/workflow/replay"),
    ]
    assert summary == {
        "schema_version": "live_smoke_workflow_start.v1",
        "status": "started",
        "response_status_code": 200,
        "run_id": "run-a",
        "run_state": "running",
        "current_stage": "DRAFT",
        "started_by": "live-smoke",
        "discussion_session_present": False,
        "replay": {
            "schema_version": "live_smoke_workflow_replay_summary.v1",
            "status": "pass",
            "event_count": 1,
            "event_log_checksum": "sha256:replay",
            "replayed_state": "running",
            "replayed_stage": "DRAFT",
            "current_state": "running",
            "current_stage": "DRAFT",
            "issues": [],
        },
    }


def test_live_episode_smoke_invocation_summary_is_rerunnable_and_redacted() -> None:
    summary = smoke_invocation_summary(
        [
            "/srv/DialectiCore/scripts/live_episode_smoke.py",
            "--production-target",
            "native_visual",
            "--api-key",
            "secret-a",
            "--openrouter-api-key=secret-b",
            "--artifact-output-dir",
            "output/smoke/live episode artifacts",
            "--no-auto-approve",
        ]
    )

    assert summary == {
        "schema_version": "live_episode_smoke_invocation.v1",
        "script": "live_episode_smoke.py",
        "argv": [
            "live_episode_smoke.py",
            "--production-target",
            "native_visual",
            "--api-key",
            "<redacted>",
            "--openrouter-api-key=<redacted>",
            "--artifact-output-dir",
            "output/smoke/live episode artifacts",
            "--no-auto-approve",
        ],
        "args": [
            "--production-target",
            "native_visual",
            "--api-key",
            "<redacted>",
            "--openrouter-api-key=<redacted>",
            "--artifact-output-dir",
            "output/smoke/live episode artifacts",
            "--no-auto-approve",
        ],
        "rerun_command": (
            "python scripts/live_episode_smoke.py --production-target native_visual "
            "--api-key '<redacted>' '--openrouter-api-key=<redacted>' "
            "--artifact-output-dir 'output/smoke/live episode artifacts' --no-auto-approve"
        ),
    }


def test_live_episode_smoke_treats_duplicate_workflow_start_as_already_running() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/workflow/start"):
            return httpx.Response(422, json={"detail": "episode workflow run is already active"})
        if request.method == "GET" and request.url.path == "/api/v1/episodes/episode-a":
            return httpx.Response(
                200,
                json={
                    "id": "episode-a",
                    "discussion_session": {"id": "discussion-a"},
                    "workflow_control": {
                        "run": {
                            "run_id": "run-a",
                            "state": "running",
                            "current_stage": "DISCUSSING",
                            "started_by": "operator",
                        }
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/workflow/replay"):
            return httpx.Response(
                200,
                json={
                    "status": "pass",
                    "event_count": 4,
                    "event_log_checksum": "sha256:replay",
                    "replayed": {"state": "running", "current_stage": "DISCUSSING"},
                    "current": {"state": "running", "current_stage": "DISCUSSING"},
                    "issues": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = ensure_workflow_started(
            client,
            "http://test",
            "episode-a",
            user_id="live-smoke",
        )

    assert summary["status"] == "already_running"
    assert summary["response_status_code"] == 422
    assert summary["run_id"] == "run-a"
    assert summary["run_state"] == "running"
    assert summary["current_stage"] == "DISCUSSING"
    assert summary["discussion_session_present"] is True
    assert summary["replay"]["status"] == "pass"


def test_live_episode_smoke_cleanup_guard_only_allows_created_start_only_runs() -> None:
    args = Namespace(cleanup_start_only_run=True, max_advances=0)
    result = {
        "created_episode": True,
        "workflow_start": {"status": "started"},
    }
    episode = {
        "status": "DRAFT",
        "workflow_control": {"run": {"state": "running"}},
    }

    assert should_cleanup_start_only_run(args, result, episode) is True
    assert (
        should_cleanup_start_only_run(
            Namespace(cleanup_start_only_run=False, max_advances=0),
            result,
            episode,
        )
        is False
    )
    assert (
        should_cleanup_start_only_run(
            Namespace(cleanup_start_only_run=True, max_advances=1),
            result,
            episode,
        )
        is False
    )
    assert (
        should_cleanup_start_only_run(
            args,
            {**result, "created_episode": False},
            episode,
        )
        is False
    )
    assert (
        should_cleanup_start_only_run(
            args,
            {**result, "workflow_start": {"status": "already_running"}},
            episode,
        )
        is False
    )


def test_live_episode_smoke_error_cleanup_guard_allows_started_created_start_only() -> None:
    args = Namespace(cleanup_start_only_run=True, max_advances=0)
    result = {
        "created_episode": True,
        "episode_id": "episode-a",
        "workflow_start": {"status": "started"},
    }

    assert should_cleanup_start_only_run_after_error(args, result) is True
    assert (
        should_cleanup_start_only_run_after_error(
            Namespace(cleanup_start_only_run=False, max_advances=0),
            result,
        )
        is False
    )
    assert (
        should_cleanup_start_only_run_after_error(
            Namespace(cleanup_start_only_run=True, max_advances=1),
            result,
        )
        is False
    )
    assert (
        should_cleanup_start_only_run_after_error(
            args,
            {**result, "created_episode": False},
        )
        is False
    )
    assert (
        should_cleanup_start_only_run_after_error(
            args,
            {**result, "workflow_start": {"status": "already_running"}},
        )
        is False
    )
    assert (
        should_cleanup_start_only_run_after_error(
            args,
            {**result, "episode_id": ""},
        )
        is False
    )


def test_live_episode_smoke_cleanup_cancels_started_run_and_records_evidence() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.method == "POST"
        assert request.url.path == "/api/v1/episodes/episode-a/workflow/actions"
        return httpx.Response(
            200,
            json={
                "id": "episode-a",
                "status": "CANCELLED",
                "workflow_control": {
                    "cancelled": True,
                    "run": {"run_id": "run-a", "state": "cancelled"},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        cleanup = cleanup_started_workflow(
            client,
            "http://test",
            "episode-a",
            user_id="live-smoke",
        )

    assert requests == [("POST", "/api/v1/episodes/episode-a/workflow/actions")]
    assert cleanup == {
        "schema_version": "live_smoke_start_only_cleanup.v1",
        "status": "cancelled",
        "episode_id": "episode-a",
        "episode_status": "CANCELLED",
        "run_id": "run-a",
        "run_state": "cancelled",
        "cancelled": True,
    }


def test_live_episode_smoke_error_cleanup_records_failure_summary() -> None:
    cleanup = cleanup_started_workflow_after_error(
        "http://127.0.0.1:1",
        "episode-a",
        user_id="live-smoke",
    )

    assert cleanup["schema_version"] == "live_smoke_start_only_cleanup.v1"
    assert cleanup["status"] == "failed"
    assert cleanup["episode_id"] == "episode-a"
    assert "error" in cleanup


def test_live_episode_smoke_provider_preflight_cleanup_guard_only_allows_created_drafts() -> None:
    args = Namespace(cleanup_preflight_draft=True)
    result = {"created_episode": True}
    episode = {"status": "DRAFT", "workflow_control": {}}

    assert should_cleanup_provider_preflight_draft(args, result, episode) is True
    assert (
        should_cleanup_provider_preflight_draft(
            Namespace(cleanup_preflight_draft=False),
            result,
            episode,
        )
        is False
    )
    assert (
        should_cleanup_provider_preflight_draft(
            args,
            {**result, "created_episode": False},
            episode,
        )
        is False
    )
    assert (
        should_cleanup_provider_preflight_draft(
            args,
            {**result, "workflow_start": {"status": "started"}},
            episode,
        )
        is False
    )
    assert (
        should_cleanup_provider_preflight_draft(
            args,
            result,
            {"status": "DRAFT", "workflow_control": {"run": {"state": "running"}}},
        )
        is False
    )
    assert (
        should_cleanup_provider_preflight_draft(
            args,
            result,
            {"status": "READY", "workflow_control": {}},
        )
        is False
    )


def test_live_episode_smoke_provider_preflight_cleanup_cancels_draft() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert json.loads(request.content)["action"] == "cancel"
        return httpx.Response(
            200,
            json={
                "id": "episode-a",
                "status": "CANCELLED",
                "workflow_control": {"cancelled": True},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        cleanup = cleanup_provider_preflight_draft(
            client,
            "http://test",
            "episode-a",
            user_id="live-smoke",
        )

    assert requests == [("POST", "/api/v1/episodes/episode-a/workflow/actions")]
    assert cleanup == {
        "schema_version": "live_smoke_provider_preflight_cleanup.v1",
        "status": "cancelled",
        "episode_id": "episode-a",
        "episode_status": "CANCELLED",
        "workflow_started": False,
        "cancelled": True,
    }


def test_live_episode_smoke_participant_id_parsing_rejects_empty_and_duplicates() -> None:
    assert parse_participant_ids("claude, chatgpt,deepseek") == [
        "claude",
        "chatgpt",
        "deepseek",
    ]
    with pytest.raises(ValueError, match="at least one participant ID"):
        parse_participant_ids(" , ")
    with pytest.raises(ValueError, match="duplicate participant ID"):
        parse_participant_ids("claude,chatgpt,claude")


def test_live_episode_smoke_builds_moderator_first_six_character_cast() -> None:
    participant_ids = ["claude", "chatgpt", "deepseek", "grok", "gemini", "mistral"]

    entries = build_episode_participant_entries(participant_ids, moderator_id="claude")

    assert entries == [
        {"participant_profile_id": "claude", "role": "moderator"},
        {"participant_profile_id": "chatgpt", "role": "panelist"},
        {"participant_profile_id": "deepseek", "role": "panelist"},
        {"participant_profile_id": "grok", "role": "panelist"},
        {"participant_profile_id": "gemini", "role": "panelist"},
        {"participant_profile_id": "mistral", "role": "panelist"},
    ]


def test_live_episode_smoke_rejects_moderator_outside_cast() -> None:
    with pytest.raises(ValueError, match="moderator ID must be included"):
        build_episode_participant_entries(["chatgpt", "claude"], moderator_id="gemini")


def test_live_episode_smoke_enables_and_restores_synthetic_mock_endpoints() -> None:
    endpoint_records = {
        "/api/v1/voicebox-endpoints/mock-voicebox": {
            "id": "mock-voicebox",
            "name": "Mock Voicebox",
            "adapter_type": "mock",
            "base_url": None,
            "credential_reference": None,
            "default_timeout_seconds": 60,
            "max_concurrency": 2,
            "retry_policy": {"max_attempts": 3},
            "enabled": False,
            "capabilities": {"tts": True},
            "health_status": "healthy",
        },
        "/api/v1/comfyui-endpoints/mock-comfyui": {
            "id": "mock-comfyui",
            "name": "Mock ComfyUI",
            "adapter_type": "mock",
            "base_url": None,
            "credential_reference": None,
            "default_timeout_seconds": 120,
            "max_concurrency": 1,
            "retry_policy": {"max_attempts": 2},
            "enabled": False,
            "capabilities": {"visual_generation": True},
            "health_status": "healthy",
        },
    }
    updates: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path in endpoint_records:
            return httpx.Response(200, json=endpoint_records[path])
        if request.method == "PUT" and path in endpoint_records:
            payload = json.loads(request.content)
            updates.append((path, payload))
            endpoint_records[path] = {**endpoint_records[path], **payload}
            return httpx.Response(200, json=endpoint_records[path])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        restore_records = enable_synthetic_mock_endpoints(client, "http://test")
        setup = synthetic_endpoint_setup_summary(restore_records)
        restored = restore_synthetic_mock_endpoints(client, "http://test", restore_records)

    assert setup == {
        "schema_version": "synthetic_mock_endpoint_setup.v1",
        "status": "pass",
        "endpoints": [
            {
                "scope": "voicebox",
                "endpoint_id": "mock-voicebox",
                "previous_enabled": False,
                "previous_health_status": "healthy",
            },
            {
                "scope": "comfyui",
                "endpoint_id": "mock-comfyui",
                "previous_enabled": False,
                "previous_health_status": "healthy",
            },
        ],
    }
    assert restored["status"] == "pass"
    assert len(restored["restored"]) == 2
    assert updates[0] == (
        "/api/v1/voicebox-endpoints/mock-voicebox",
        {
            **endpoint_update_payload(restore_records[0]["endpoint"]),
            "enabled": True,
            "health_status": "healthy",
        },
    )
    assert updates[1][0] == "/api/v1/comfyui-endpoints/mock-comfyui"
    assert updates[-2][1]["enabled"] is False
    assert updates[-1][1]["enabled"] is False


def test_live_episode_smoke_enables_and_restores_synthetic_mock_participants() -> None:
    participant_records = {
        "/api/v1/participant-profiles/host": {
            "id": "host",
            "name": "host",
            "display_name": "Moderator",
            "participant_type": "host",
            "model_endpoint_id": "mock",
            "model_id": "mock-host-v1",
            "system_prompt_template": "moderator_v1",
            "perspective": "balanced",
            "expertise": "moderation",
            "speaking_style": "concise",
            "sampling_settings": {"temperature": 0.4, "top_p": 0.95, "max_tokens": 350},
            "tool_policy_id": "no_tools",
            "voice_profile_id": "voice-host",
            "visual_profile_id": "visual-host",
            "enabled": False,
        },
        "/api/v1/participant-profiles/optimist": {
            "id": "optimist",
            "name": "optimist",
            "display_name": "The Optimist",
            "participant_type": "panelist",
            "model_endpoint_id": "mock",
            "model_id": "mock-optimist-v1",
            "system_prompt_template": "panelist_v1",
            "perspective": "adoption",
            "expertise": "tooling",
            "speaking_style": "constructive",
            "sampling_settings": {"temperature": 0.7, "top_p": 0.95, "max_tokens": 500},
            "tool_policy_id": "no_tools",
            "voice_profile_id": "voice-optimist",
            "visual_profile_id": "visual-optimist",
            "enabled": False,
        },
    }
    updates: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path in participant_records:
            return httpx.Response(200, json=participant_records[path])
        if request.method == "PUT" and path in participant_records:
            payload = json.loads(request.content)
            updates.append((path, payload))
            participant_records[path] = {**participant_records[path], **payload}
            return httpx.Response(200, json=participant_records[path])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        restore_records = enable_synthetic_mock_participants(
            client,
            "http://test",
            ["host", "optimist"],
        )
        setup = synthetic_participant_setup_summary(restore_records)
        restored = restore_synthetic_mock_participants(client, "http://test", restore_records)

    assert setup["schema_version"] == "synthetic_mock_participant_setup.v1"
    assert setup["participants"] == [
        {
            "participant_id": "host",
            "previous_enabled": False,
            "model_endpoint_id": "mock",
            "voice_profile_id": "voice-host",
            "visual_profile_id": "visual-host",
        },
        {
            "participant_id": "optimist",
            "previous_enabled": False,
            "model_endpoint_id": "mock",
            "voice_profile_id": "voice-optimist",
            "visual_profile_id": "visual-optimist",
        },
    ]
    assert restored["status"] == "pass"
    assert updates[0] == (
        "/api/v1/participant-profiles/host",
        {
            **participant_update_payload(restore_records[0]["participant"]),
            "enabled": True,
        },
    )
    assert updates[1][0] == "/api/v1/participant-profiles/optimist"
    assert updates[-2][1]["enabled"] is False
    assert updates[-1][1]["enabled"] is False


def test_live_episode_smoke_repoints_and_restores_synthetic_mock_workflows() -> None:
    workflow_records = {
        "/api/v1/comfyui-workflows/workflow-a": {
            "id": "workflow-a",
            "name": "Workflow A",
            "workflow_type": "talking_head",
            "version": "v1",
            "comfyui_endpoint_id": "b1-comfyui",
            "output_asset_type": "video",
            "api_workflow": {"1": {"class_type": "SaveVideo"}},
            "prompt_template": {},
            "default_parameters": {},
            "enabled": True,
        },
        "/api/v1/comfyui-workflows/workflow-b": {
            "id": "workflow-b",
            "name": "Workflow B",
            "workflow_type": "studio_wide_shot",
            "version": "v1",
            "comfyui_endpoint_id": "mock-comfyui",
            "output_asset_type": "studio_scene",
            "api_workflow": {},
            "prompt_template": {},
            "default_parameters": {},
            "enabled": True,
        },
        "/api/v1/comfyui-workflows/workflow-disabled": {
            "id": "workflow-disabled",
            "name": "Workflow Disabled",
            "workflow_type": "image_edit",
            "version": "v1",
            "comfyui_endpoint_id": "b1-comfyui",
            "output_asset_type": "image",
            "api_workflow": {},
            "prompt_template": {},
            "default_parameters": {},
            "enabled": False,
        },
    }
    updates: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/comfyui-workflows":
            return httpx.Response(200, json=list(workflow_records.values()))
        if request.method == "PUT" and path in workflow_records:
            payload = json.loads(request.content)
            updates.append((path, payload))
            workflow_records[path] = {**workflow_records[path], **payload}
            return httpx.Response(200, json=workflow_records[path])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        restore_records = enable_synthetic_mock_workflows(client, "http://test")
        setup = synthetic_workflow_setup_summary(restore_records)
        restored = restore_synthetic_mock_workflows(client, "http://test", restore_records)

    assert [item["workflow_id"] for item in setup["workflows"]] == [
        "workflow-a",
        "workflow-b",
    ]
    assert restored["status"] == "pass"
    assert updates[0] == (
        "/api/v1/comfyui-workflows/workflow-a",
        {
            **workflow_update_payload(restore_records[0]["workflow"]),
            "comfyui_endpoint_id": "mock-comfyui",
        },
    )
    assert updates[-2][1]["comfyui_endpoint_id"] == "b1-comfyui"
    assert updates[-1][1]["comfyui_endpoint_id"] == "mock-comfyui"


def test_live_episode_smoke_create_episode_uses_configured_six_character_cast() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/episodes":
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "episode-a", "status": "DRAFT"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        episode = create_episode(
            client,
            "http://test",
            "Six character smoke",
            production_target="audio_first",
            target_duration_minutes=2,
            permitted_deviation_percent=50,
            participant_ids=[
                "claude",
                "chatgpt",
                "deepseek",
                "grok",
                "gemini",
                "mistral",
            ],
            moderator_id="claude",
            research_enabled=True,
        )

    definition = captured_payload["definition"]
    assert episode["id"] == "episode-a"
    assert definition["format"]["participant_count"] == 6
    assert definition["participants"] == [
        {"participant_profile_id": "claude", "role": "moderator"},
        {"participant_profile_id": "chatgpt", "role": "panelist"},
        {"participant_profile_id": "deepseek", "role": "panelist"},
        {"participant_profile_id": "grok", "role": "panelist"},
        {"participant_profile_id": "gemini", "role": "panelist"},
        {"participant_profile_id": "mistral", "role": "panelist"},
    ]
    assert definition["workflow"]["production_target"] == "audio_first"
    assert definition["research"]["enabled"] is True


def test_live_episode_smoke_cast_readiness_summarizes_selected_character_config() -> None:
    episode = {
        "definition": {
            "participants": [
                {"participant_profile_id": "claude", "role": "moderator"},
                {"participant_profile_id": "chatgpt", "role": "panelist"},
            ]
        }
    }

    summary = cast_readiness_summary(
        episode,
        participants=[
            {
                "id": "claude",
                "display_name": "Claude",
                "participant_type": "host",
                "enabled": True,
                "model_endpoint_id": "openrouter",
                "model_id": "anthropic/claude-sonnet-5",
                "voice_profile_id": "voice-claude",
                "visual_profile_id": "visual-claude",
            },
            {
                "id": "chatgpt",
                "display_name": "ChatGPT",
                "participant_type": "panelist",
                "enabled": True,
                "model_endpoint_id": "openrouter",
                "model_id": "openai/gpt-4.1-mini",
                "voice_profile_id": "voice-chatgpt",
                "visual_profile_id": "visual-chatgpt",
            },
        ],
        model_endpoints=[
            {
                "id": "openrouter",
                "enabled": True,
                "health_status": "healthy",
                "credential_reference": "env:OPENROUTER_API_KEY",
            }
        ],
        voice_profiles=[
            {"id": "voice-claude", "name": "A_DE_Claude", "enabled": True},
            {"id": "voice-chatgpt", "name": "A_ChatGPT", "enabled": True},
        ],
        visual_profiles=[
            {
                "id": "visual-claude",
                "name": "Claude Visual",
                "enabled": True,
                "primary_workflow_id": "workflow-talking-head-v1",
            },
            {
                "id": "visual-chatgpt",
                "name": "ChatGPT Visual",
                "enabled": True,
                "primary_workflow_id": "workflow-talking-head-v1",
            },
        ],
        fallback_participant_ids=[],
        fallback_moderator_id="claude",
    )

    assert summary["schema_version"] == "cast_readiness_smoke_summary.v1"
    assert summary["status"] == "pass"
    assert summary["participant_count"] == 2
    assert summary["configured_participant_count"] == 2
    assert summary["moderator_count"] == 1
    assert summary["issues"] == []
    assert summary["participants"][0] == {
        "participant_id": "claude",
        "display_name": "Claude",
        "role": "moderator",
        "participant_type": "host",
        "ready": True,
        "enabled": True,
        "model_endpoint_id": "openrouter",
        "model_endpoint_enabled": True,
        "model_endpoint_health_status": "healthy",
        "model_id": "anthropic/claude-sonnet-5",
        "voice_profile_id": "voice-claude",
        "voice_profile_name": "A_DE_Claude",
        "voice_profile_enabled": True,
        "visual_profile_id": "visual-claude",
        "visual_profile_name": "Claude Visual",
        "visual_profile_enabled": True,
        "primary_workflow_id": "workflow-talking-head-v1",
        "issues": [],
    }
    payload = json.dumps(summary)
    assert "env:OPENROUTER_API_KEY" not in payload


def test_live_episode_smoke_cast_readiness_fails_missing_and_disabled_config() -> None:
    summary = cast_readiness_summary(
        {
            "definition": {
                "participants": [
                    {"participant_profile_id": "claude", "role": "moderator"},
                    {"participant_profile_id": "gemini", "role": "panelist"},
                ]
            }
        },
        participants=[
            {
                "id": "claude",
                "display_name": "Claude",
                "enabled": False,
                "model_endpoint_id": "missing-endpoint",
                "model_id": "",
                "voice_profile_id": "disabled-voice",
                "visual_profile_id": "missing-visual",
            }
        ],
        model_endpoints=[],
        voice_profiles=[{"id": "disabled-voice", "enabled": False}],
        visual_profiles=[],
        fallback_participant_ids=[],
        fallback_moderator_id="claude",
    )

    assert summary["status"] == "fail"
    assert summary["configured_participant_count"] == 0
    assert summary["issues"] == [
        "claude:participant_disabled",
        "claude:model_endpoint_unknown",
        "claude:model_id_missing",
        "claude:voice_profile_disabled",
        "claude:visual_profile_unknown",
        "gemini:participant_profile_missing",
    ]
    assert summary["participants"][1] == {
        "participant_id": "gemini",
        "role": "panelist",
        "ready": False,
        "issues": ["participant_profile_missing"],
    }


def test_live_episode_smoke_cast_readiness_fetch_failure_is_actionable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/participant-profiles":
            return httpx.Response(503, text="not ready")
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = get_cast_readiness_summary(
            client,
            "http://test",
            {"definition": {}},
            fallback_participant_ids=["claude"],
            fallback_moderator_id="claude",
        )

    assert summary["schema_version"] == "cast_readiness_smoke_summary.v1"
    assert summary["status"] == "fail"
    assert summary["ready"] is False
    assert summary["issues"] == ["cast_configuration_unavailable:HTTPStatusError"]


def test_live_episode_smoke_provider_preflight_summarizes_model_and_voice_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/participant-profiles":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "chatgpt",
                        "display_name": "ChatGPT",
                        "model_endpoint_id": "openrouter",
                        "model_id": "openai/gpt-4.1-mini",
                        "voice_profile_id": "voice-chatgpt",
                    },
                    {
                        "id": "claude",
                        "display_name": "Claude",
                        "model_endpoint_id": "openrouter",
                        "model_id": "anthropic/claude-sonnet-5",
                        "voice_profile_id": "voice-claude",
                    },
                ],
            )
        if request.url.path == "/api/v1/model-endpoints":
            return httpx.Response(200, json=[{"id": "openrouter"}])
        if request.url.path == "/api/v1/voice-profiles":
            return httpx.Response(200, json=[{"id": "voice-chatgpt"}, {"id": "voice-claude"}])
        if request.url.path == "/api/v1/voicebox-endpoints":
            return httpx.Response(200, json=[{"id": "b1-voicebox"}])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    monkeypatch.setattr(
        "scripts.live_provider_smoke.run_all_participant_model_smokes",
        lambda *, participants, model_endpoints: [
            {
                "status": "pass",
                "participant_id": "chatgpt",
                "model_id": "openai/gpt-4.1-mini",
            },
            {
                "status": "pass",
                "participant_id": "claude",
                "model_id": "anthropic/claude-sonnet-5",
            },
        ],
    )
    monkeypatch.setattr(
        "scripts.live_provider_smoke.run_all_participant_voice_smokes",
        lambda *, participants, voice_profiles, voice_endpoints, text, output_path: [
            {
                "status": "pass",
                "participant_id": "chatgpt",
                "voice_profile_id": "voice-chatgpt",
            },
            {
                "status": "fail",
                "participant_id": "claude",
                "voice_profile_id": "voice-claude",
                "status_code": 500,
            },
        ],
    )
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = provider_smoke_preflight_summary(
            client,
            "http://test",
            participant_ids=["chatgpt", "claude"],
            voice_output_path=tmp_path / "voice.wav",
        )

    assert summary["schema_version"] == "live_episode_provider_smoke_preflight.v1"
    assert summary["status"] == "fail"
    assert summary["participant_ids"] == ["chatgpt", "claude"]
    assert summary["model_summary"]["failed_count"] == 0
    assert summary["voicebox_summary"]["failed_count"] == 1
    assert summary["voicebox_summary"]["failed_participant_ids"] == ["claude"]
    assert summary["blockers"] == ["one or more selected participant voice smokes failed"]


def test_live_episode_smoke_provider_preflight_appends_voicebox_requirements(
    tmp_path: Path,
) -> None:
    requirements_path = tmp_path / "media-requirements.md"
    provider_preflight = {
        "schema_version": "live_episode_provider_smoke_preflight.v1",
        "status": "fail",
        "participant_ids": ["chatgpt", "claude"],
        "voicebox_participants": [
            {
                "status": "pass",
                "participant_id": "chatgpt",
                "voice_profile_id": "voice-chatgpt",
            },
            {
                "status": "fail",
                "participant_id": "claude",
                "voice_profile_id": "voice-claude",
                "endpoint_id": "b1-voicebox",
                "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
                "engine": "chatterbox",
                "status_code": 500,
                "content_type": "text/plain; charset=utf-8",
                "bytes": 21,
                "riff_wave": False,
                "action": "fix_voicebox_generation_then_rerun_health_check",
            },
        ],
        "voicebox_summary": {
            "failed_count": 1,
            "failed_participant_ids": ["claude"],
        },
    }

    update = append_provider_preflight_requirements(
        requirements_path,
        {"api_base": "http://test", "episode_id": "episode-a"},
        provider_preflight,
    )

    assert update == {
        "path": str(requirements_path),
        "appended": True,
        "source": "live_episode_provider_smoke_preflight",
        "failed_voice_count": 1,
        "failed_participant_ids": ["claude"],
    }
    text = requirements_path.read_text(encoding="utf-8")
    assert "Voicebox Smoke Recheck Added" in text
    assert "participant_id: `chatgpt,claude`" in text
    assert "participant voice checks: `2`" in text
    assert "failed participant voices: `1`" in text
    assert "endpoint_id=`b1-voicebox`" in text
    assert "riff_wave=`False`" in text


def test_live_episode_smoke_provider_preflight_requirements_skip_non_voice_failures(
    tmp_path: Path,
) -> None:
    update = append_provider_preflight_requirements(
        tmp_path / "media-requirements.md",
        {"api_base": "http://test", "episode_id": "episode-a"},
        {
            "status": "fail",
            "participant_ids": ["chatgpt"],
            "voicebox_participants": [],
            "voicebox_summary": {"failed_count": 0, "failed_participant_ids": []},
            "model_summary": {"failed_count": 1},
        },
    )

    assert update == {
        "path": str(tmp_path / "media-requirements.md"),
        "appended": False,
        "reason": "no_voicebox_preflight_failure",
    }
    assert not (tmp_path / "media-requirements.md").exists()


def test_live_episode_smoke_provider_preflight_requirements_respects_disabled_flag(
    tmp_path: Path,
) -> None:
    update = append_provider_preflight_requirements(
        tmp_path / "media-requirements.md",
        {"api_base": "http://test", "episode_id": "episode-a"},
        {
            "status": "fail",
            "participant_ids": ["claude"],
            "voicebox_participants": [{"status": "fail", "participant_id": "claude"}],
            "voicebox_summary": {"failed_count": 1, "failed_participant_ids": ["claude"]},
        },
        disabled=True,
    )

    assert update == {
        "path": str(tmp_path / "media-requirements.md"),
        "appended": False,
        "reason": "requirements_update_disabled",
    }
    assert not (tmp_path / "media-requirements.md").exists()


def test_live_episode_smoke_discussion_speaker_coverage_requires_every_selected_character() -> None:
    cast = {
        "participants": [
            {"participant_id": "claude"},
            {"participant_id": "chatgpt"},
            {"participant_id": "gemini"},
        ]
    }
    episode = {
        "discussion_session": {
            "turns": [
                {"speaker_participant_id": "claude", "status": "completed"},
                {"speaker_participant_id": "chatgpt", "status": "completed"},
                {"speaker_participant_id": "claude", "status": "excluded"},
                {"speaker_participant_id": "other", "status": "completed"},
            ]
        }
    }

    summary = discussion_speaker_coverage_summary(episode, cast_readiness=cast)

    assert summary == {
        "schema_version": "discussion_speaker_coverage_smoke_summary.v1",
        "status": "fail",
        "selected_participant_count": 3,
        "playable_turn_count": 3,
        "covered_participant_count": 2,
        "selected_participant_ids": ["claude", "chatgpt", "gemini"],
        "spoken_participant_ids": ["chatgpt", "claude"],
        "missing_participant_ids": ["gemini"],
        "turn_count_by_participant": {
            "claude": 1,
            "chatgpt": 1,
            "gemini": 0,
        },
        "issues": ["selected_cast_speakers_missing"],
    }


def test_live_episode_smoke_discussion_speaker_coverage_passes_when_all_selected_spoke() -> None:
    summary = discussion_speaker_coverage_summary(
        {
            "discussion_session": {
                "turns": [
                    {"speaker_participant_id": "claude", "status": "completed"},
                    {"speaker_participant_id": "chatgpt", "status": "completed"},
                ]
            }
        },
        cast_readiness={
            "participants": [
                {"participant_id": "claude"},
                {"participant_id": "chatgpt"},
            ]
        },
    )

    assert summary["status"] == "pass"
    assert summary["missing_participant_ids"] == []
    assert summary["turn_count_by_participant"] == {"claude": 1, "chatgpt": 1}


def test_live_episode_smoke_start_only_success_requires_replay_and_cleanup() -> None:
    result = {
        "workflow_start": {
            "status": "started",
            "replay": {"status": "pass"},
        },
        "cleanup": {"status": "cancelled"},
    }

    assert (
        start_only_smoke_succeeded(
            Namespace(max_advances=0, cleanup_start_only_run=True),
            result,
        )
        is True
    )
    assert (
        start_only_smoke_succeeded(
            Namespace(max_advances=1, cleanup_start_only_run=True),
            result,
        )
        is False
    )
    assert (
        start_only_smoke_succeeded(
            Namespace(max_advances=0, cleanup_start_only_run=True),
            {**result, "cleanup": {"status": "unexpected"}},
        )
        is False
    )
    assert (
        start_only_smoke_succeeded(
            Namespace(max_advances=0, cleanup_start_only_run=False),
            {"workflow_start": {"status": "already_running", "replay": {"status": "pass"}}},
        )
        is True
    )


def test_live_episode_smoke_approval_auto_approve_skips_superseded_approvals() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/approval-stale/decision"):
            return httpx.Response(404, json={"detail": "approval or episode not found"})
        if request.method == "POST" and request.url.path.endswith("/approval-active/decision"):
            return httpx.Response(200, json={"id": "episode-a"})
        return httpx.Response(404)

    episode = {
        "id": "episode-a",
        "approvals": [
            {
                "id": "approval-stale",
                "stage": "preview_render_review",
                "decision": "pending",
            },
            {
                "id": "approval-active",
                "stage": "final_render_review",
                "decision": "pending",
            },
        ],
    }

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        approvals = approve_pending_gates(client, "http://test", episode, user_id="tester")

    assert requests == [
        ("POST", "/api/v1/episodes/episode-a/approvals/approval-stale/decision"),
        ("POST", "/api/v1/episodes/episode-a/approvals/approval-active/decision"),
    ]
    assert approvals == [
        {
            "stage": "preview_render_review",
            "approval_id": "approval-stale",
            "decision": "skipped",
            "reason": "approval_not_found_or_superseded",
        },
        {
            "stage": "final_render_review",
            "approval_id": "approval-active",
            "decision": "approved",
        },
    ]


def test_live_episode_smoke_calls_run_until_blocked_endpoint() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "payload": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            json={
                "episode": {"id": "episode-a", "status": "TRANSCRIPT_REVIEW"},
                "status": "awaiting_approval",
                "stop_reason": "pending_approval",
                "pass_count": 1,
                "progressed_stage_count": 1,
                "summaries": [],
                "pending_approvals": [],
                "completion_readiness": {"status": "fail"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        result = run_episode_until_blocked(
            client,
            "http://test",
            "episode-a",
            user_id="tester",
            max_passes=3,
        )

    assert result["status"] == "awaiting_approval"
    assert requests == [
        {
            "method": "POST",
            "path": "/api/v1/episodes/episode-a/workflow/run-until-blocked",
            "payload": {
                "start_if_needed": True,
                "max_passes": 3,
                "user_id": "tester",
                "comment": "Live smoke run until the next review gate or completion.",
            },
        }
    ]


def test_live_episode_smoke_run_until_blocked_summary_is_compact() -> None:
    summary = run_until_blocked_summary(
        {
            "status": "awaiting_approval",
            "stop_reason": "pending_approval",
            "pass_count": 2,
            "progressed_stage_count": 4,
            "pending_approvals": [
                {"stage": "preview_render_review", "id": "approval-a"},
            ],
            "handoff": {
                "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
                "source_schema_version": "talkshow_production_handoff.v1",
                "status": "blocked",
                "next_handoff_action": "produce_remaining_speech_assets",
                "blocking_reasons": [
                    "completed_audio_missing",
                    "completed_character_visual_missing",
                ],
                "playable_turn_count": 6,
                "character_configuration": {
                    "ready": True,
                    "missing_model_participant_ids": [],
                    "missing_voice_participant_ids": ["chatgpt"],
                    "participants": [{"raw": "not copied"}],
                },
                "turn_handoffs": {
                    "completed_audio_turn_count": 5,
                    "completed_primary_visual_turn_count": 4,
                    "missing_audio_turn_ids": ["turn-a"],
                    "missing_primary_visual_turn_ids": ["turn-a", "turn-b"],
                    "stale_voice_asset_turn_ids": ["raw-not-copied"],
                },
                "stage_readiness": {
                    "speech": False,
                    "character_animation": False,
                    "subtitles": True,
                    "timeline": False,
                    "raw_detail": "not copied",
                },
                "asset_ids": {
                    "preview_render": "preview-a",
                    "delivery_package": "package-a",
                    "raw_asset": "not copied",
                },
            },
            "summaries": [
                {
                    "stages": {
                        "render": {
                            "preview_renders_created": 1,
                            "large_raw_payload": "not copied",
                        },
                        "publishing": {"youtube_packages_created": 0},
                        "completion": {"readiness_blocked": 1},
                    }
                }
            ],
        }
    )

    assert summary == {
        "schema_version": "live_smoke_run_until_blocked_summary.v1",
        "status": "awaiting_approval",
        "stop_reason": "pending_approval",
        "pass_count": 2,
        "progressed_stage_count": 4,
        "handoff": {
            "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
            "source_schema_version": "talkshow_production_handoff.v1",
            "status": "blocked",
            "next_handoff_action": "produce_remaining_speech_assets",
            "blocking_reasons": [
                "completed_audio_missing",
                "completed_character_visual_missing",
            ],
            "playable_turn_count": 6,
            "character_configuration": {
                "ready": True,
                "missing_model_participant_ids": [],
                "missing_voice_participant_ids": ["chatgpt"],
                "missing_visual_participant_ids": [],
            },
            "turn_handoffs": {
                "completed_audio_turn_count": 5,
                "completed_primary_visual_turn_count": 4,
                "missing_audio_turn_count": 1,
                "missing_primary_visual_turn_count": 2,
            },
            "stage_readiness": {
                "speech": False,
                "character_animation": False,
                "subtitles": True,
                "timeline": False,
            },
            "asset_ids": {
                "preview_render": "preview-a",
                "delivery_package": "package-a",
            },
        },
        "pending_approval_count": 1,
        "pending_approval_stages": ["preview_render_review"],
        "latest_render": {"preview_renders_created": 1},
        "latest_publishing": {"youtube_packages_created": 0},
        "latest_completion": {"readiness_blocked": 1},
    }
    assert "participants" not in summary["handoff"]["character_configuration"]
    assert "raw_detail" not in summary["handoff"]["stage_readiness"]
    assert "raw_asset" not in summary["handoff"]["asset_ids"]


def test_live_episode_smoke_records_package_inspection_and_artifact_downloads() -> None:
    report = {
        "deliverables": {
            "final_render": {"asset_id": "render-a", "download_url": "/render"},
            "export_package": {"asset_id": "package-a", "download_url": "/package"},
            "production_manifest": {"asset_id": "manifest-a", "download_url": "/manifest"},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/youtube-package/inspect"):
            return httpx.Response(
                200,
                json={
                    "status": "pass",
                    "file_count": 4,
                    "manifest_schema_version": "youtube_package.v1",
                    "chapter_count": 2,
                    "subtitle_count": 1,
                    "evidence_source_count": 3,
                    "manifest_matches_asset_metadata": True,
                    "issues": [],
                },
            )
        return httpx.Response(
            206,
            headers={
                "content-type": "application/octet-stream",
                "content-range": "bytes 0-0/12",
                "content-disposition": 'attachment; filename="artifact.bin"',
            },
            content=b"x",
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        inspection = inspect_delivery_package(client, "http://test", "episode-a", report)
        downloads = artifact_download_evidence(client, "http://test", report)

    assert inspection == {
        "schema_version": "live_smoke_package_inspection.v1",
        "status": "pass",
        "package_asset_id": "package-a",
        "file_count": 4,
        "manifest_schema_version": "youtube_package.v1",
        "chapter_count": 2,
        "subtitle_count": 1,
        "evidence_source_count": 3,
        "manifest_matches_asset_metadata": True,
        "issues": [],
    }
    assert downloads["status"] == "pass"
    assert {check["name"] for check in downloads["checks"]} == {
        "final_render",
        "export_package",
        "production_manifest",
    }
    assert {check["response_status_code"] for check in downloads["checks"]} == {206}
    assert {check["bytes_read"] for check in downloads["checks"]} == {1}


def test_live_episode_smoke_saves_artifact_downloads(tmp_path: Path) -> None:
    artifact_bytes = {
        "/render": b"mp4-bytes",
        "/package": b"zip-bytes",
        "/manifest": b"json-bytes",
    }
    report = {
        "deliverables": {
            "final_render": {
                "asset_id": "render-a",
                "download_url": "/render",
                "checksum": (
                    "sha256:"
                    "225e2e71f6963695684cf5c2aef7d582fff76acb8c028ed8b79c9c52bc93495d"
                ),
            },
            "export_package": {"asset_id": "package-a", "download_url": "/package"},
            "production_manifest": {"asset_id": "manifest-a", "download_url": "/manifest"},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=artifact_bytes[request.url.path],
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        downloads = artifact_download_evidence(
            client,
            "http://test",
            report,
            tmp_path / "artifacts",
        )

    assert downloads["status"] == "pass"
    assert downloads["artifact_output_dir"] == str(tmp_path / "artifacts")
    saved_paths = {Path(check["saved_path"]).name for check in downloads["checks"]}
    assert saved_paths == {
        "final-render.mp4",
        "youtube-export-package.zip",
        "production-manifest.json",
    }
    assert (tmp_path / "artifacts" / "final-render.mp4").read_bytes() == b"mp4-bytes"
    assert all(check["bytes_read"] > 0 for check in downloads["checks"])


def test_live_episode_smoke_saved_artifact_checksum_mismatch_fails(tmp_path: Path) -> None:
    report = {
        "deliverables": {
            "final_render": {
                "asset_id": "render-a",
                "download_url": "/render",
                "checksum": "sha256:not-the-real-digest",
            },
            "export_package": {"asset_id": "package-a", "download_url": "/package"},
            "production_manifest": {"asset_id": "manifest-a", "download_url": "/manifest"},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"artifact")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        downloads = artifact_download_evidence(
            client,
            "http://test",
            report,
            tmp_path / "artifacts",
        )

    assert downloads["status"] == "fail"
    failed = [check for check in downloads["checks"] if check["name"] == "final_render"][0]
    assert failed["status"] == "fail"
    assert failed["issues"] == ["checksum_mismatch"]
    assert "final_render:fail" in downloads["issues"]


def test_live_episode_smoke_marks_missing_download_url_as_failed() -> None:
    report = {
        "deliverables": {
            "final_render": {"asset_id": "render-a", "download_url": "/render"},
            "export_package": {
                "asset_id": "package-a",
                "download_missing_reason": "stored_object_not_found",
            },
            "production_manifest": {"asset_id": "manifest-a", "download_url": "/manifest"},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, content=b"x")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        downloads = artifact_download_evidence(client, "http://test", report)

    assert downloads["status"] == "fail"
    failed = [check for check in downloads["checks"] if check["name"] == "export_package"][0]
    assert failed["status"] == "fail"
    assert failed["reason"] == "stored_object_not_found"
    assert "export_package:fail" in downloads["issues"]


def test_live_episode_smoke_package_inspection_records_http_failure() -> None:
    report = {"deliverables": {"export_package": {"asset_id": "package-a"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": {"issues": ["package_file_missing"]}})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        inspection = inspect_delivery_package(client, "http://test", "episode-a", report)

    assert inspection["status"] == "fail"
    assert inspection["response_status_code"] == 422
    assert json.loads(inspection["error"]) == {"detail": {"issues": ["package_file_missing"]}}


def test_live_episode_smoke_production_acceptance_summary_is_compact() -> None:
    report = {
        "status": "pass",
        "production_target": "audio_first",
        "production_target_satisfied": True,
        "blockers": [],
        "acceptance_summary": {
            "publish_evidence": {
                "status": "warning",
                "publish_job_id": "publish-a",
                "job_status": "completed",
                "dry_run": True,
                "package_asset_matches": True,
                "payload_package_matches": True,
                "current_manifest_embeds_publish_job": True,
                "current_manifest_publish_job_status_matches": True,
                "payload_manifest_is_current": False,
                "payload_production_manifest_schema_version": "production_manifest.v1",
                "publish_url": "mock://not/copied",
                "payload": {"raw": "not-copied"},
            }
        },
        "workflow_run_until_blocked": {
            "schema_version": "production_workflow_run_until_blocked_summary.v1",
            "source_schema_version": "workflow_run_until_blocked_evidence.v1",
            "recorded_at": "2026-07-30T12:00:00+00:00",
            "status": "completed",
            "stop_reason": "completed",
            "pass_count": 4,
            "progressed_stage_count": 4,
            "pending_approval_count": 0,
            "pending_approval_stages": [],
            "completion_status": "pass",
            "completion_failed_checks": [],
            "orchestration_attempt_count": 11,
            "orchestration_attempt_ids": [f"attempt-{index}" for index in range(12)],
            "summaries": [{"raw": "not-copied"}],
        },
        "deliverables": {
            "final_render": {
                "asset_id": "render-a",
                "status": "completed",
                "checksum": "sha256:render",
                "mime_type": "video/mp4",
                "downloadable": True,
                "file_size_bytes": 123,
                "download_url": "/large/url/not/copied",
                "storage_uri": "object://not/copied",
            },
            "export_package": {
                "asset_id": "package-a",
                "status": "completed",
                "checksum": "sha256:package",
                "mime_type": "application/zip",
                "downloadable": True,
                "file_size_bytes": 456,
            },
            "production_manifest": {
                "asset_id": "manifest-a",
                "status": "completed",
                "checksum": "sha256:manifest",
                "mime_type": "application/json",
                "downloadable": True,
                "file_size_bytes": 789,
            },
        },
    }
    summary = production_acceptance_summary(
        episode={"id": "episode-a", "status": "COMPLETED"},
        completion={"status": "pass", "failed_checks": []},
        production_test_report=report,
        package_inspection={
            "status": "pass",
            "package_asset_id": "package-a",
            "file_count": 4,
            "manifest_schema_version": "youtube_package.v1",
            "chapter_count": 2,
            "subtitle_count": 1,
            "evidence_source_count": 3,
            "manifest_matches_asset_metadata": True,
            "issues": [],
        },
        artifact_download_checks={
            "status": "pass",
            "checks": [
                {
                    "name": "export_package",
                    "status": "pass",
                    "asset_id": "package-a",
                    "response_status_code": 206,
                    "bytes_read": 1,
                    "content_type": "application/zip",
                    "content_range": "bytes 0-0/456",
                    "content_disposition": 'attachment; filename="package.zip"',
                }
            ],
        },
        discussion_speaker_coverage={
            "status": "pass",
            "selected_participant_count": 2,
            "covered_participant_count": 2,
            "playable_turn_count": 4,
            "missing_participant_ids": [],
            "turn_count_by_participant": {"claude": 2, "chatgpt": 2},
            "issues": [],
        },
    )

    assert summary["schema_version"] == "production_acceptance_summary.v1"
    assert summary["status"] == "pass"
    assert summary["deliverables"]["final_render"] == {
        "asset_id": "render-a",
        "status": "completed",
        "checksum": "sha256:render",
        "mime_type": "video/mp4",
        "downloadable": True,
        "file_size_bytes": 123,
        "download_missing_reason": None,
    }
    assert "download_url" not in summary["deliverables"]["final_render"]
    assert summary["package"]["manifest_schema_version"] == "youtube_package.v1"
    assert summary["downloads"]["export_package"] == {
        "status": "pass",
        "asset_id": "package-a",
        "response_status_code": 206,
        "bytes_read": 1,
        "content_type": "application/zip",
        "content_range": "bytes 0-0/456",
    }
    assert summary["discussion_speaker_coverage"] == {
        "status": "pass",
        "selected_participant_count": 2,
        "covered_participant_count": 2,
        "playable_turn_count": 4,
        "missing_participant_ids": [],
        "turn_count_by_participant": {"claude": 2, "chatgpt": 2},
        "issues": [],
    }
    assert summary["publish_evidence"] == {
        "status": "warning",
        "publish_job_id": "publish-a",
        "job_status": "completed",
        "dry_run": True,
        "package_asset_matches": True,
        "payload_package_matches": True,
        "current_manifest_embeds_publish_job": True,
        "current_manifest_publish_job_status_matches": True,
        "payload_manifest_is_current": False,
        "payload_production_manifest_schema_version": "production_manifest.v1",
    }
    assert "publish_url" not in summary["publish_evidence"]
    assert "payload" not in summary["publish_evidence"]
    assert summary["workflow_run_until_blocked"] == {
        "schema_version": "production_workflow_run_until_blocked_summary.v1",
        "source_schema_version": "workflow_run_until_blocked_evidence.v1",
        "recorded_at": "2026-07-30T12:00:00+00:00",
        "status": "completed",
        "stop_reason": "completed",
        "pass_count": 4,
        "progressed_stage_count": 4,
        "pending_approval_count": 0,
        "pending_approval_stages": [],
        "completion_status": "pass",
        "completion_failed_checks": [],
        "handoff": {
            "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
            "status": "missing",
            "blocking_reasons": [],
        },
        "orchestration_attempt_count": 11,
        "orchestration_attempt_ids": [f"attempt-{index}" for index in range(10)],
    }
    assert "summaries" not in summary["workflow_run_until_blocked"]


def test_live_episode_smoke_acceptance_fails_when_selected_speaker_coverage_is_incomplete() -> None:
    summary = production_acceptance_summary(
        episode={"id": "episode-a", "status": "COMPLETED"},
        completion={"status": "pass", "failed_checks": []},
        production_test_report={
            "status": "pass",
            "production_target": "audio_first",
            "production_target_satisfied": True,
            "blockers": [],
            "deliverables": {},
            "real_life_test_readiness": {
                "schema_version": "production_real_life_test_readiness.v1",
                "ready": False,
                "audio_first_ready": False,
                "native_visual_ready": False,
                "live_provider_preflight_ready": False,
                "managed_media_smoke_ready": False,
                "audio_first_blockers": [
                    "fix_voicebox_generation_then_rerun_live_preflight"
                ],
                "native_visual_blockers": [
                    "local_acceptance_not_ready",
                    "fix_voicebox_generation_then_rerun_live_preflight",
                ],
                "next_action": "rerun_live_provider_preflight_after_provider_fix",
            },
        },
        package_inspection={"status": "pass"},
        artifact_download_checks={"status": "pass", "checks": []},
        discussion_speaker_coverage={
            "status": "fail",
            "selected_participant_count": 3,
            "covered_participant_count": 2,
            "playable_turn_count": 6,
            "missing_participant_ids": ["gemini"],
            "turn_count_by_participant": {"claude": 3, "chatgpt": 3, "gemini": 0},
            "issues": ["selected_cast_speakers_missing"],
        },
    )

    assert summary["status"] == "fail"
    assert summary["discussion_speaker_coverage_status"] == "fail"
    assert "selected_cast_speaker_coverage_incomplete" in summary["blockers"]
    assert summary["discussion_speaker_coverage"]["missing_participant_ids"] == ["gemini"]
    assert summary["real_life_test_readiness"]["status"] == "fail"
    assert summary["real_life_test_readiness"]["audio_first_blockers"] == [
        "fix_voicebox_generation_then_rerun_live_preflight"
    ]


def test_live_episode_smoke_compacts_real_life_test_readiness() -> None:
    report = {
        "real_life_test_readiness": {
            "schema_version": "production_real_life_test_readiness.v1",
            "ready": False,
            "recommended_mode": None,
            "audio_first_ready": False,
            "native_visual_ready": False,
            "live_provider_preflight_ready": False,
            "managed_media_smoke_ready": False,
            "audio_first_blockers": [
                "fix_voicebox_generation_then_rerun_live_preflight",
                "extra-a",
            ],
            "native_visual_blockers": [
                "local_acceptance_not_ready",
                "fix_voicebox_generation_then_rerun_live_preflight",
                "fix_b1_managed_media_runner_then_rerun_smoke",
                "extra-1",
                "extra-2",
                "extra-3",
                "extra-4",
                "extra-5",
                "extra-6",
            ],
            "next_action": "rerun_live_provider_preflight_after_provider_fix",
        }
    }

    compact = compact_real_life_test_readiness(report)

    assert compact == {
        "schema_version": "production_real_life_test_readiness_summary.v1",
        "source_schema_version": "production_real_life_test_readiness.v1",
        "status": "fail",
        "ready": False,
        "recommended_mode": None,
        "audio_first_ready": False,
        "native_visual_ready": False,
        "live_provider_preflight_ready": False,
        "managed_media_smoke_ready": False,
        "audio_first_blockers": [
            "fix_voicebox_generation_then_rerun_live_preflight",
            "extra-a",
        ],
        "native_visual_blockers": [
            "local_acceptance_not_ready",
            "fix_voicebox_generation_then_rerun_live_preflight",
            "fix_b1_managed_media_runner_then_rerun_smoke",
            "extra-1",
            "extra-2",
            "extra-3",
            "extra-4",
            "extra-5",
        ],
        "next_action": "rerun_live_provider_preflight_after_provider_fix",
    }


def test_live_episode_smoke_production_test_summary_includes_real_life_readiness() -> None:
    summary = production_test_summary(
        {
            "status": "pass",
            "production_target": "audio_first",
            "production_target_satisfied": True,
            "audio_first_test_ready": True,
            "native_visual_test_ready": False,
            "publish": {"status": "completed"},
            "blockers": [],
            "operator_next_action": "inspect_export_package_and_publish_evidence",
            "real_life_test_readiness": {
                "schema_version": "production_real_life_test_readiness.v1",
                "ready": False,
                "audio_first_ready": False,
                "native_visual_ready": False,
                "live_provider_preflight_ready": False,
                "managed_media_smoke_ready": False,
                "audio_first_blockers": [
                    "fix_voicebox_generation_then_rerun_live_preflight"
                ],
                "native_visual_blockers": [
                    "local_acceptance_not_ready",
                    "fix_voicebox_generation_then_rerun_live_preflight",
                    "fix_b1_managed_media_runner_then_rerun_smoke",
                ],
                "next_action": "rerun_live_provider_preflight_after_provider_fix",
            },
        }
    )

    assert summary["status"] == "pass"
    assert summary["real_life_test_readiness"] == {
        "schema_version": "production_real_life_test_readiness_summary.v1",
        "source_schema_version": "production_real_life_test_readiness.v1",
        "status": "fail",
        "ready": False,
        "recommended_mode": None,
        "audio_first_ready": False,
        "native_visual_ready": False,
        "live_provider_preflight_ready": False,
        "managed_media_smoke_ready": False,
        "audio_first_blockers": [
            "fix_voicebox_generation_then_rerun_live_preflight"
        ],
        "native_visual_blockers": [
            "local_acceptance_not_ready",
            "fix_voicebox_generation_then_rerun_live_preflight",
            "fix_b1_managed_media_runner_then_rerun_smoke",
        ],
        "next_action": "rerun_live_provider_preflight_after_provider_fix",
    }


def test_live_episode_smoke_provider_requirements_skips_clean_report(tmp_path: Path) -> None:
    update = append_episode_provider_requirements(
        tmp_path / "media-requirements.md",
        {
            "api_base": "http://test",
            "episode_id": "episode-a",
        },
        production_test_report={
            "status": "pass",
            "production_target": "audio_first",
            "media_readiness": {
                "audio_operator_action": "audio_generation_ready",
                "audio_generation": {"status": "pass", "provider_ready": True},
                "managed_media_operator_action": "no_managed_media_action_required",
                "managed_media_execution": {"status": "not_required"},
                "managed_media_smoke": {"status": "missing"},
                "managed_media_missing_preset_endpoints": [],
            },
        },
        live_provider_readiness={
            "status": "pass",
            "checks": [{"category": "comfyui", "status": "pass"}],
        },
    )

    assert update == {
        "path": str(tmp_path / "media-requirements.md"),
        "appended": False,
        "reason": "no_b1_provider_issue",
    }
    assert not (tmp_path / "media-requirements.md").exists()


def test_live_episode_smoke_provider_requirements_appends_b1_handoff(
    tmp_path: Path,
) -> None:
    report = {
        "status": "warning",
        "production_target": "native_visual",
        "operator_next_action": "fix_b1_managed_media_runner_then_retry_visual_assets",
        "media_readiness": {
            "audio_operator_action": "fix_voicebox_generation_then_retry_audio_assets",
            "audio_generation": {
                "status": "fail",
                "provider_ready": False,
                "failed_count": 1,
                "voicebox_asset_count": 3,
                "provider_issue_samples": [
                    {
                        "endpoint_id": "b1-voicebox",
                        "health_status": "unhealthy",
                        "adapter_type": "b1_voice_stream",
                        "canary_status": "fail",
                        "canary_status_code": 500,
                        "canary_riff_wave": False,
                    }
                ],
                "failure_samples": [
                    {
                        "asset_id": "audio-a",
                        "voicebox_endpoint_id": "b1-voicebox",
                        "voice_profile_id": "voice-claude",
                        "remote_profile_id": "remote-claude",
                        "failure_type": "HTTPStatusError",
                        "failure": "500 Internal Server Error",
                    }
                ],
            },
            "managed_media_operator_action": (
                "fix_b1_managed_media_runner_then_retry_visual_assets"
            ),
            "managed_media_execution": {
                "status": "fail",
                "required": True,
                "failed_count": 1,
                "fallback_visual_count": 0,
                "failure_samples": [
                    {
                        "asset_id": "visual-a",
                        "model": "video-image",
                        "operation": "image-to-video",
                        "provider_state": "failed",
                        "failure_category": "gpu_runner_error",
                        "failure_message": "ValueError",
                    }
                ],
            },
            "managed_media_smoke": {
                "status": "runner_failed",
                "model": "image-default",
                "operation": "image-generation",
                "terminal_state": "failed",
                "failure_category": "gpu_runner_error",
            },
            "managed_media_missing_preset_endpoints": [
                {
                    "endpoint": {"id": "b1-comfyui", "name": "B1 ComfyUI"},
                    "required_presets": ["video-image"],
                    "missing_presets": ["video-image"],
                    "available_presets": ["image-default"],
                }
            ],
        },
    }
    live_provider_readiness = {
        "status": "fail",
        "checks": [
            {
                "category": "comfyui",
                "status": "fail",
                "blockers": ["one or more ComfyUI endpoints are unhealthy"],
                "failed_readiness_checks": ["comfyui_endpoints_not_unhealthy"],
                "unhealthy_endpoints": [
                    {
                        "id": "b1-comfyui",
                        "name": "B1 ComfyUI",
                        "adapter_type": "comfyui_http",
                        "health_status": "unhealthy",
                    }
                ],
            }
        ],
    }

    issues = episode_provider_requirement_issues(
        production_test_report=report,
        live_provider_readiness=live_provider_readiness,
    )
    assert issues["voicebox"]
    assert issues["managed_media"]
    assert issues["comfyui"]

    requirements_path = tmp_path / "media-requirements.md"
    update = append_episode_provider_requirements(
        requirements_path,
        {
            "api_base": "http://test",
            "episode_id": "episode-a",
        },
        production_test_report=report,
        live_provider_readiness=live_provider_readiness,
    )

    assert update == {
        "path": str(requirements_path),
        "appended": True,
        "voicebox_issue_count": len(issues["voicebox"]),
        "managed_media_issue_count": len(issues["managed_media"]),
        "comfyui_issue_count": len(issues["comfyui"]),
    }
    text = requirements_path.read_text(encoding="utf-8")
    assert "DialectiCore Production Provider Handoff Added" in text
    assert "Voicebox issues:" in text
    assert "B1 managed-media issues:" in text
    assert "Native ComfyUI gateway issues:" in text
    assert "endpoint_id=`b1-voicebox`" in text
    assert "model=`video-image`" in text
    assert "missing_presets=`['video-image']`" in text
    assert "scripts/live_episode_smoke.py" in text


def test_live_episode_smoke_refreshes_report_after_handoff_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get_json(client: httpx.Client, url: str) -> dict:
        del client
        calls.append(url)
        return {
            "status": "warning",
            "production_target": "native_visual",
            "production_target_satisfied": False,
            "audio_first_test_ready": False,
            "native_visual_test_ready": False,
            "publish": {"status": "missing"},
            "blockers": ["publish_job_missing"],
            "operator_next_action": "fix_b1_managed_media_runner_then_rerun_smoke",
            "provider_repair_handoff": {
                "status": "present",
                "path": "/home/mordred/media-requirements.md",
            },
        }

    monkeypatch.setattr("scripts.live_episode_smoke.get_json", fake_get_json)
    result = {
        "api_base": "http://test",
        "episode_id": "episode-a",
        "requirements_update": {"path": "/home/mordred/media-requirements.md"},
    }

    refreshed = refresh_production_test_report_after_handoff(
        result,
        {"status": "warning", "provider_repair_handoff": {"status": "missing"}},
    )

    assert calls == ["http://test/api/v1/episodes/episode-a/production-test-report"]
    assert refreshed["provider_repair_handoff"]["status"] == "present"
    assert result["requirements_update"]["report_refresh"] == {
        "status": "pass",
        "provider_repair_handoff_status": "present",
    }


def test_live_episode_smoke_appends_requirements_and_refreshes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_refresh(
        result: dict,
        current_report: dict,
    ) -> dict:
        result["requirements_update"]["report_refresh"] = {
            "status": "pass",
            "provider_repair_handoff_status": "present",
        }
        return {
            **current_report,
            "provider_repair_handoff": {
                "status": "present",
                "path": result["requirements_update"]["path"],
            },
        }

    monkeypatch.setattr(
        "scripts.live_episode_smoke.refresh_production_test_report_after_handoff",
        fake_refresh,
    )
    result = {"api_base": "http://test", "episode_id": "episode-a"}
    report = {
        "status": "warning",
        "production_target": "native_visual",
        "media_readiness": {
            "managed_media_operator_action": (
                "fix_b1_managed_media_runner_then_rerun_smoke"
            ),
            "managed_media_execution": {"status": "not_attempted"},
            "managed_media_smoke": {
                "status": "runner_failed",
                "model": "image-default",
                "operation": "image-generation",
                "terminal_state": "failed",
                "failure_category": "gpu_runner_error",
            },
        },
    }

    refreshed = append_provider_requirements_and_refresh_report(
        result,
        report,
        live_provider_readiness={"status": "warning", "checks": []},
        requirements_output=tmp_path / "media-requirements.md",
    )

    assert result["requirements_update"]["appended"] is True
    assert result["requirements_update"]["report_refresh"]["status"] == "pass"
    assert refreshed["provider_repair_handoff"]["status"] == "present"
    assert "B1 managed-media issues:" in (
        tmp_path / "media-requirements.md"
    ).read_text(encoding="utf-8")


def test_live_episode_smoke_requirements_refresh_respects_disabled_flag(
    tmp_path: Path,
) -> None:
    result = {"api_base": "http://test", "episode_id": "episode-a"}
    report = {
        "status": "warning",
        "media_readiness": {
            "managed_media_operator_action": (
                "fix_b1_managed_media_runner_then_rerun_smoke"
            ),
            "managed_media_smoke": {"status": "runner_failed"},
        },
    }

    returned = append_provider_requirements_and_refresh_report(
        result,
        report,
        live_provider_readiness={"status": "warning", "checks": []},
        requirements_output=tmp_path / "media-requirements.md",
        disabled=True,
    )

    assert returned is report
    assert "requirements_update" not in result
    assert not (tmp_path / "media-requirements.md").exists()


def test_live_episode_smoke_production_test_summary_is_stable() -> None:
    summary = production_test_summary(
        {
            "status": "warning",
            "production_target": "native_visual",
            "production_target_satisfied": False,
            "audio_first_test_ready": True,
            "native_visual_test_ready": False,
            "publish": {"status": "completed", "payload": {"not": "copied"}},
            "blockers": ["native_visual_missing"],
            "operator_next_action": "retry_fallback_visuals_as_native_after_b1_fix",
            "provider_repair_handoff": {"path": "/home/mordred/media-requirements.md"},
        }
    )

    assert summary == {
        "status": "warning",
        "production_target": "native_visual",
        "production_target_satisfied": False,
        "audio_first_test_ready": True,
        "native_visual_test_ready": False,
        "publish_status": "completed",
        "blockers": ["native_visual_missing"],
        "operator_next_action": "retry_fallback_visuals_as_native_after_b1_fix",
        "real_life_test_readiness": {
            "schema_version": "production_real_life_test_readiness_summary.v1",
            "status": "missing",
            "ready": False,
        },
    }


def test_live_episode_smoke_native_visual_preflight_summary_is_compact() -> None:
    summary = native_visual_preflight_summary(
        {
            "pilot_modes": [
                {
                    "mode": "native_visual",
                    "status": "fail",
                    "blockers": [
                        "one or more selected native ComfyUI endpoints block prompt admission"
                    ],
                    "warnings": [],
                }
            ],
            "stages": [
                {
                    "category": "visuals",
                    "status": "fail",
                    "details": {
                        "readiness_checks": {
                            "selected_native_comfyui_prompt_admission_ready": False
                        },
                        "prompt_admission_blocked_endpoints": [
                            {
                                "endpoint": {
                                    "id": "b1-comfyui",
                                    "name": "B1 Native ComfyUI",
                                    "adapter_type": "comfyui_http",
                                    "health_status": "unhealthy",
                                    "base_url_configured": True,
                                    "prompt_admission": {
                                        "ready": False,
                                        "status_code": 503,
                                        "code": "hardware_resource_policy",
                                        "message": "GPU admission blocked",
                                        "detail": "free VRAM below reserve",
                                    },
                                },
                                "participant_ids": ["claude", "chatgpt"],
                                "workflow_ids": ["workflow-talking-head-v1"],
                                "visual_profile_ids": ["visual-claude", "visual-chatgpt"],
                            }
                        ],
                    },
                    "blockers": ["visual blocker"],
                    "warnings": ["visual warning"],
                }
            ],
        }
    )

    assert summary == {
        "schema_version": "native_visual_preflight_summary.v1",
        "status": "fail",
        "visual_stage_status": "fail",
        "native_visual_mode_status": "fail",
        "prompt_admission_ready": False,
        "prompt_admission_blocked_endpoint_count": 1,
        "prompt_admission_blocked_endpoints": [
            {
                "endpoint_id": "b1-comfyui",
                "endpoint_name": "B1 Native ComfyUI",
                "health_status": "unhealthy",
                "admission_ready": False,
                "admission_status_code": 503,
                "admission_code": "hardware_resource_policy",
                "admission_message": "GPU admission blocked",
                "admission_detail": "free VRAM below reserve",
                "participant_ids": ["claude", "chatgpt"],
                "workflow_ids": ["workflow-talking-head-v1"],
                "visual_profile_ids": ["visual-claude", "visual-chatgpt"],
            }
        ],
        "blockers": [
            "one or more selected native ComfyUI endpoints block prompt admission"
        ],
        "warnings": [],
    }


def test_live_episode_smoke_compacts_live_provider_readiness_without_secret_surfaces() -> None:
    summary = live_provider_readiness_summary(
        {
            "status": "fail",
            "checked_at": "2026-07-30T10:10:00+00:00",
            "summary": {"check_count": 22, "fail_count": 2},
            "checks": [
                {
                    "category": "voicebox",
                    "status": "fail",
                    "label": "Voicebox endpoints",
                    "blockers": ["one or more enabled voicebox endpoints records are unhealthy"],
                    "warnings": [],
                    "details": {
                        "configured": 3,
                        "enabled": 2,
                        "healthy": 0,
                        "unhealthy": 1,
                        "unknown": 0,
                        "failed_readiness_checks": ["no_unhealthy_voicebox_endpoints"],
                        "unhealthy_endpoints": [
                            {
                                "id": "b1-voicebox",
                                "name": "B1 Voicebox Native Stream",
                                "adapter_type": "b1_voice_stream",
                                "health_status": "unhealthy",
                                "credential_reference": "env:B1_API_KEY",
                                "voice_generation": {
                                    "ready": False,
                                    "status": "fail",
                                    "status_code": 500,
                                    "content_type": "text/plain; charset=utf-8",
                                    "bytes": 21,
                                    "riff_wave": False,
                                    "profile_id": (
                                        "bd4e9bf1-482b-4900-97c1-48275d1ba28c"
                                    ),
                                    "engine": "chatterbox",
                                    "action": (
                                        "fix_voicebox_generation_then_rerun_health_check"
                                    ),
                                },
                            }
                        ],
                    },
                },
                {
                    "category": "managed_media_smoke",
                    "status": "fail",
                    "label": "B1 managed media smoke",
                    "blockers": ["latest B1 managed media smoke did not complete successfully"],
                    "warnings": [],
                    "details": {
                        "status": "runner_failed",
                        "model": "image-default",
                        "modality": "image",
                        "operation": "image-generation",
                        "terminal_state": "failed",
                        "failure_category": "gpu_runner_error",
                        "action": "fix_b1_managed_media_runner_then_rerun_smoke",
                        "failed_readiness_checks": ["managed_media_smoke_passed"],
                    },
                },
                {
                    "category": "credential_provisioning",
                    "status": "warning",
                    "label": "Credential Provisioning",
                    "blockers": [],
                    "warnings": ["not needed in compact smoke summary"],
                    "details": {},
                },
            ],
        }
    )

    assert summary["schema_version"] == "live_provider_readiness_smoke_summary.v1"
    assert summary["status"] == "fail"
    assert summary["checked_at"] == "2026-07-30T10:10:00+00:00"
    assert [check["category"] for check in summary["checks"]] == [
        "voicebox",
        "managed_media_smoke",
    ]
    assert summary["checks"][0]["unhealthy_endpoints"] == [
        {
            "id": "b1-voicebox",
            "name": "B1 Voicebox Native Stream",
            "adapter_type": "b1_voice_stream",
            "health_status": "unhealthy",
            "voice_generation": {
                "ready": False,
                "status": "fail",
                "status_code": 500,
                "content_type": "text/plain; charset=utf-8",
                "bytes": 21,
                "riff_wave": False,
                "engine": "chatterbox",
                "action": "fix_voicebox_generation_then_rerun_health_check",
            },
        }
    ]
    assert summary["checks"][1]["failure_category"] == "gpu_runner_error"
    payload = json.dumps(summary)
    assert "env:B1_API_KEY" not in payload
    assert "bd4e9bf1-482b-4900-97c1-48275d1ba28c" not in payload


def test_live_episode_smoke_records_live_provider_readiness_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "GET"
            and request.url.path == "/api/v1/system/live-provider-readiness"
        ):
            return httpx.Response(503, text="not ready")
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = get_live_provider_readiness_summary(client, "http://test")

    assert summary["schema_version"] == "live_provider_readiness_smoke_summary.v1"
    assert summary["status"] == "fail"
    assert summary["checks"] == []
    assert summary["blockers"] == ["live_provider_readiness_unavailable"]


def test_live_episode_smoke_native_visual_preflight_stops_unless_ignored() -> None:
    pilot = {"production_target": "native_visual"}
    summary = {"status": "fail"}

    assert (
        native_visual_preflight_should_stop(
            args=Namespace(
                production_target="native_visual",
                ignore_native_visual_preflight_blockers=False,
            ),
            pilot=pilot,
            preflight_summary=summary,
        )
        is True
    )
    assert (
        native_visual_preflight_should_stop(
            args=Namespace(
                production_target="native_visual",
                ignore_native_visual_preflight_blockers=True,
            ),
            pilot=pilot,
            preflight_summary=summary,
        )
        is False
    )
    assert (
        native_visual_preflight_should_stop(
            args=Namespace(
                production_target="audio_first",
                ignore_native_visual_preflight_blockers=False,
            ),
            pilot=pilot,
            preflight_summary=summary,
        )
        is False
    )


def test_live_episode_smoke_waits_only_for_native_visual_blocked_preflight() -> None:
    blocked_pilot = {"production_target": "native_visual"}
    blocked_summary = {"status": "fail"}

    assert should_wait_for_native_visual_admission(
        Namespace(
            production_target="native_visual",
            ignore_native_visual_preflight_blockers=False,
            wait_native_visual_admission_seconds=30,
        ),
        blocked_pilot,
        blocked_summary,
    )
    assert not should_wait_for_native_visual_admission(
        Namespace(
            production_target="native_visual",
            ignore_native_visual_preflight_blockers=True,
            wait_native_visual_admission_seconds=30,
        ),
        blocked_pilot,
        blocked_summary,
    )
    assert not should_wait_for_native_visual_admission(
        Namespace(
            production_target="audio_first",
            ignore_native_visual_preflight_blockers=False,
            wait_native_visual_admission_seconds=30,
        ),
        blocked_pilot,
        blocked_summary,
    )
    assert not should_wait_for_native_visual_admission(
        Namespace(
            production_target="native_visual",
            ignore_native_visual_preflight_blockers=False,
            wait_native_visual_admission_seconds=0,
        ),
        blocked_pilot,
        {"status": "pass"},
    )


def test_live_episode_smoke_wait_for_native_visual_admission_records_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []
    pilot_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pilot_calls
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/v1/comfyui-endpoints":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "b1-comfyui",
                        "adapter_type": "comfyui_http",
                        "enabled": True,
                        "capabilities": {"native_comfyui": True},
                    }
                ],
            )
        if (
            request.method == "POST"
            and request.url.path == "/api/v1/comfyui-endpoints/b1-comfyui/health"
        ):
            return httpx.Response(
                200,
                json={
                    "id": "b1-comfyui",
                    "health_status": "healthy",
                    "capabilities": {
                        "native_comfyui": True,
                        "prompt_admission_ready": True,
                    },
                },
            )
        if (
            request.method == "GET"
            and request.url.path == "/api/v1/episodes/episode-a/pilot-readiness"
        ):
            pilot_calls += 1
            return httpx.Response(
                200,
                json={
                    "production_target": "native_visual",
                    "pilot_modes": [
                        {
                            "mode": "native_visual",
                            "status": "pass" if pilot_calls > 1 else "fail",
                            "blockers": [] if pilot_calls > 1 else ["blocked"],
                            "warnings": [],
                        }
                    ],
                    "stages": [],
                },
            )
        return httpx.Response(404)

    times = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    monkeypatch.setattr("scripts.live_episode_smoke.monotonic", lambda: next(times, 0.5))
    monkeypatch.setattr("scripts.live_episode_smoke.sleep", lambda _seconds: None)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        result = wait_for_native_visual_admission(
            client,
            "http://test",
            "episode-a",
            max_wait_seconds=5,
            interval_seconds=1,
        )

    assert result["schema_version"] == "native_visual_admission_wait.v1"
    assert result["status"] == "pass"
    assert result["attempt_count"] == 2
    assert [attempt["status"] for attempt in result["attempts"]] == ["fail", "pass"]
    assert requests.count(("POST", "/api/v1/comfyui-endpoints/b1-comfyui/health")) == 2


def test_live_episode_smoke_refreshes_non_mock_comfyui_health() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/v1/comfyui-endpoints":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "mock-comfyui",
                        "adapter_type": "mock",
                        "enabled": True,
                        "capabilities": {},
                    },
                    {
                        "id": "b1-comfyui",
                        "adapter_type": "comfyui_http",
                        "enabled": True,
                        "capabilities": {"native_comfyui": True},
                    },
                    {
                        "id": "disabled-comfyui",
                        "adapter_type": "comfyui_http",
                        "enabled": False,
                        "capabilities": {"native_comfyui": True},
                    },
                ],
            )
        if (
            request.method == "POST"
            and request.url.path == "/api/v1/comfyui-endpoints/b1-comfyui/health"
        ):
            return httpx.Response(
                200,
                json={
                    "id": "b1-comfyui",
                    "health_status": "unhealthy",
                    "capabilities": {
                        "native_comfyui": True,
                        "prompt_admission_ready": False,
                        "prompt_admission_probe": {
                            "ready": False,
                            "status_code": 503,
                            "response": {
                                "detail": {
                                    "code": "hardware_resource_policy",
                                    "message": "GPU admission blocked",
                                    "hardware_resource_policy": {
                                        "detail": "free VRAM below reserve"
                                    },
                                }
                            },
                        },
                    },
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        refresh = refresh_comfyui_health(client, "http://test")

    assert requests == [
        ("GET", "/api/v1/comfyui-endpoints"),
        ("POST", "/api/v1/comfyui-endpoints/b1-comfyui/health"),
    ]
    assert refresh == {
        "schema_version": "live_smoke_comfyui_health_refresh.v1",
        "status": "pass",
        "candidate_endpoint_count": 1,
        "refreshed": [
            {
                "endpoint_id": "b1-comfyui",
                "status": "pass",
                "health_status": "unhealthy",
                "native_comfyui": True,
                "prompt_admission_ready": False,
                "prompt_admission": {
                    "ready": False,
                    "status_code": 503,
                    "code": "hardware_resource_policy",
                    "message": "GPU admission blocked",
                    "detail": "free VRAM below reserve",
                },
            }
        ],
        "issues": [],
    }


def test_live_episode_smoke_comfyui_health_refresh_records_list_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        refresh = refresh_comfyui_health(client, "http://test")

    assert refresh["schema_version"] == "live_smoke_comfyui_health_refresh.v1"
    assert refresh["status"] == "fail"
    assert refresh["refreshed"] == []
    assert refresh["issues"] == ["list_comfyui_endpoints:HTTPStatusError"]
