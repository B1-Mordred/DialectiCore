from pathlib import Path

import httpx
import pytest

from scripts.live_provider_smoke import (
    FRONTIER_CAST_PARTICIPANT_IDS,
    append_voicebox_requirements,
    filter_participants,
    frontier_cast_scope_enabled,
    parse_participant_ids,
    participant_voice_output_path,
    participant_voice_scope_ids,
    readiness_summary,
    run_all_participant_model_smokes,
    run_all_participant_voice_smokes,
    run_voice_smoke,
    voicebox_participant_summary,
    write_evidence,
)


def test_live_provider_voice_smoke_records_b1_stream_failure_without_writing_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = {
        "id": "b1-voicebox",
        "name": "B1 Voicebox",
        "adapter_type": "b1_voice_stream",
        "base_url": "https://voice.ai.b1.germering",
        "capabilities": {
            "stream_generation_path": "/generate/stream",
            "default_engine": "chatterbox",
        },
    }
    profile = {
        "id": "voice-claude",
        "name": "Claude",
        "voice_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        "language": "de",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/generate/stream"
        return httpx.Response(
            500,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=b"Internal Server Error",
        )

    transport = httpx.MockTransport(handler)

    class MockClient(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(transport=transport)

    monkeypatch.setattr("scripts.live_provider_smoke.httpx.Client", MockClient)

    output_path = tmp_path / "speech.wav"
    result = run_voice_smoke(
        endpoint=endpoint,
        profile=profile,
        text="Guten Tag.",
        output_path=output_path,
    )

    assert result == {
        "schema_version": "voicebox_stream_smoke_evidence.v1",
        "endpoint_id": "b1-voicebox",
        "endpoint_name": "B1 Voicebox",
        "adapter_type": "b1_voice_stream",
        "url": "https://voice.ai.b1.germering/generate/stream",
        "voice_profile_id": "voice-claude",
        "voice_name": "Claude",
        "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        "language": "de",
        "engine": "chatterbox",
        "status_code": 500,
        "content_type": "text/plain; charset=utf-8",
        "bytes": 21,
        "riff_wave": False,
        "status": "fail",
        "action": "fix_voicebox_generation_then_rerun_health_check",
    }
    assert not output_path.exists()


def test_live_provider_evidence_file_and_requirements_append(tmp_path: Path) -> None:
    result = {
        "schema_version": "live_provider_smoke_evidence.v1",
        "participant_id": "claude",
        "voicebox": {
            "endpoint_id": "b1-voicebox",
            "endpoint_name": "B1 Voicebox",
            "url": "https://voice.ai.b1.germering/generate/stream",
            "profile_id": "profile-remote",
            "voice_profile_id": "voice-claude",
            "engine": "chatterbox",
            "status": "fail",
            "status_code": 500,
            "content_type": "text/plain",
            "bytes": 21,
            "riff_wave": False,
            "action": "fix_voicebox_generation_then_rerun_health_check",
        },
    }

    evidence = write_evidence(tmp_path / "provider.json", result)
    requirements = append_voicebox_requirements(tmp_path / "requirements.md", result)

    assert evidence["path"].endswith("provider.json")
    assert evidence["bytes"] > 0
    assert evidence["sha256"]
    assert requirements == {"path": str(tmp_path / "requirements.md"), "appended": True}
    text = (tmp_path / "requirements.md").read_text(encoding="utf-8")
    assert "Voicebox Smoke Recheck Added" in text
    assert "profile-remote" in text
    assert "fix_voicebox_generation_then_rerun_health_check" in text


def test_live_provider_all_voice_summary_and_requirements_are_operator_safe(
    tmp_path: Path,
) -> None:
    result = {
        "schema_version": "live_provider_smoke_evidence.v1",
        "participant_id": "chatgpt",
        "voicebox_participants": [
            {
                "participant_id": "chatgpt",
                "voice_profile_id": "voice-chatgpt",
                "endpoint_id": "b1-voicebox",
                "profile_id": "remote-chatgpt",
                "engine": "chatterbox",
                "status": "pass",
                "status_code": 200,
                "content_type": "audio/wav",
                "bytes": 2048,
                "riff_wave": True,
            },
            {
                "participant_id": "claude",
                "voice_profile_id": "voice-claude",
                "endpoint_id": "b1-voicebox",
                "profile_id": "remote-claude",
                "engine": "chatterbox",
                "status": "fail",
                "status_code": 500,
                "content_type": "text/plain",
                "bytes": 21,
                "riff_wave": False,
                "failure_message": "private traceback must not render",
                "action": "fix_voicebox_generation_then_rerun_health_check",
            },
        ],
    }

    summary = voicebox_participant_summary(result["voicebox_participants"])
    requirements = append_voicebox_requirements(tmp_path / "requirements.md", result)

    assert summary == {
        "schema_version": "voicebox_participant_smoke_summary.v1",
        "participant_count": 2,
        "pass_count": 1,
        "failed_count": 1,
        "failed_participant_ids": ["claude"],
        "failed_voice_profile_ids": ["voice-claude"],
    }
    assert requirements == {"path": str(tmp_path / "requirements.md"), "appended": True}
    text = (tmp_path / "requirements.md").read_text(encoding="utf-8")
    assert "participant voice checks: `2`" in text
    assert "failed participant voices: `1`" in text
    assert "participant_id=`claude`" in text
    assert "profile_id=`remote-claude`" in text
    assert "private traceback" not in text


def test_live_provider_all_voice_output_paths_are_per_participant(tmp_path: Path) -> None:
    base = tmp_path / "live-provider-smoke.wav"

    assert participant_voice_output_path(base, "A_DE_Claude") == (
        tmp_path / "live-provider-smoke-a_de_claude.wav"
    )
    assert participant_voice_output_path(base, "bad id!").name == (
        "live-provider-smoke-bad-id.wav"
    )


def test_live_provider_participant_id_filter_preserves_requested_order() -> None:
    participants = [
        {"id": "chatgpt"},
        {"id": "claude"},
        {"id": "mistral"},
    ]

    assert parse_participant_ids("mistral, claude") == ["mistral", "claude"]
    assert filter_participants(participants, ["mistral", "claude"]) == [
        {"id": "mistral"},
        {"id": "claude"},
    ]

    with pytest.raises(ValueError, match="participant profile\\(s\\) not found: grok"):
        filter_participants(participants, ["grok"])


def test_live_provider_frontier_cast_voice_scope_is_explicit_and_ordered() -> None:
    assert participant_voice_scope_ids([], frontier_cast=True) == FRONTIER_CAST_PARTICIPANT_IDS
    assert participant_voice_scope_ids([], frontier_cast=False) == []
    assert participant_voice_scope_ids(["mistral", "claude"], frontier_cast=True) == [
        "mistral",
        "claude",
    ]


def test_live_provider_frontier_cast_scope_accepts_generic_and_legacy_flags() -> None:
    class Args:
        frontier_cast = False
        frontier_cast_voices = False

    args = Args()
    assert frontier_cast_scope_enabled(args) is False
    args.frontier_cast = True
    assert frontier_cast_scope_enabled(args) is True
    args.frontier_cast = False
    args.frontier_cast_voices = True
    assert frontier_cast_scope_enabled(args) is True


def test_live_provider_all_voice_smoke_keeps_going_after_bad_endpoint(
    tmp_path: Path,
) -> None:
    results = run_all_participant_voice_smokes(
        participants=[
            {
                "id": "claude",
                "display_name": "Claude",
                "voice_profile_id": "voice-claude",
                "model_endpoint_id": "openrouter",
                "model_id": "anthropic/claude-sonnet-4",
            }
        ],
        voice_profiles=[
            {
                "id": "voice-claude",
                "name": "Claude",
                "voicebox_endpoint_id": "bad-voicebox",
                "voice_id": "remote-claude",
                "language": "de",
            }
        ],
        voice_endpoints=[
            {
                "id": "bad-voicebox",
                "name": "Bad Voicebox",
                "adapter_type": "b1_voice_stream",
                "base_url": "",
            }
        ],
        text="Guten Tag.",
        output_path=tmp_path / "speech.wav",
    )

    assert results == [
        {
            "schema_version": "voicebox_stream_smoke_evidence.v1",
            "status": "fail",
            "voice_profile_id": "voice-claude",
            "voice_name": "Claude",
            "profile_id": "remote-claude",
            "endpoint_id": "bad-voicebox",
            "endpoint_name": "Bad Voicebox",
            "url": None,
            "failure_reason": "voice_smoke_exception",
            "error_type": "ValueError",
            "error": "Voicebox endpoint has no base_url",
            "action": "fix_voice_profile_or_voicebox_endpoint_configuration",
            "participant_id": "claude",
            "participant_name": "Claude",
            "model_endpoint_id": "openrouter",
            "model_id": "anthropic/claude-sonnet-4",
        }
    ]


def test_live_provider_all_model_smoke_keeps_going_after_bad_endpoint() -> None:
    results = run_all_participant_model_smokes(
        participants=[
            {
                "id": "claude",
                "display_name": "Claude",
                "model_endpoint_id": "missing-openrouter",
                "model_id": "anthropic/claude-sonnet-5",
            }
        ],
        model_endpoints=[],
    )

    assert results == [
        {
            "status": "fail",
            "endpoint_id": "missing-openrouter",
            "model_id": "anthropic/claude-sonnet-5",
            "error_type": "ValueError",
            "error": "model endpoint missing-openrouter was not found",
            "action": "fix_model_endpoint_or_participant_model_configuration",
            "participant_id": "claude",
            "participant_name": "Claude",
        }
    ]


def test_live_provider_readiness_summary_includes_voicebox_failures() -> None:
    summary = readiness_summary(
        {
            "status": "fail",
            "checks": [
                {
                    "category": "voicebox",
                    "status": "fail",
                    "details": {
                        "unhealthy_endpoints": [
                            {
                                "id": "b1-voicebox",
                                "voice_generation": {"status_code": 500},
                            }
                        ]
                    },
                },
                {"category": "comfyui", "status": "pass", "details": {}},
            ],
        }
    )

    assert summary["status"] == "fail"
    assert summary["voicebox_status"] == "fail"
    assert summary["voicebox_unhealthy_endpoints"] == [
        {"id": "b1-voicebox", "voice_generation": {"status_code": 500}}
    ]
