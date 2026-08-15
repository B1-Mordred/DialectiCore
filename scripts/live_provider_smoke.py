#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_TEXT = "Guten Tag. DialectiCore prueft jetzt eine echte Stimme fuer den Pilottest."
FRONTIER_CAST_PARTICIPANT_IDS = ["chatgpt", "claude", "deepseek", "grok", "gemini", "mistral"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a small real-provider smoke without exposing credentials."
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--participant-id", default="chatgpt")
    parser.add_argument(
        "--participant-ids",
        default="",
        help=(
            "Comma-separated participant IDs to include with participant-wide "
            "model and voice checks."
        ),
    )
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--output",
        default="output/smoke/live-provider-smoke.wav",
        help="Where to write the generated voice sample.",
    )
    parser.add_argument(
        "--evidence-output",
        default="",
        help="Optional path where the JSON smoke result is written.",
    )
    parser.add_argument(
        "--requirements-output",
        default="",
        help=(
            "Optional markdown file to append B1 Voicebox failure requirements to "
            "when the voice smoke fails."
        ),
    )
    parser.add_argument(
        "--skip-openrouter",
        action="store_true",
        help="Only check API readiness and B1 voice generation.",
    )
    parser.add_argument(
        "--all-participant-models",
        action="store_true",
        help=(
            "Run model smoke generation for every selected participant instead of "
            "only --participant-id."
        ),
    )
    parser.add_argument(
        "--skip-voice",
        action="store_true",
        help="Only check API readiness and OpenRouter generation.",
    )
    parser.add_argument(
        "--all-participant-voices",
        action="store_true",
        help=(
            "Run Voicebox smoke generation for every participant with a configured "
            "voice profile instead of only --participant-id."
        ),
    )
    parser.add_argument(
        "--frontier-cast-voices",
        action="store_true",
        help=(
            "Deprecated alias for --frontier-cast."
        ),
    )
    parser.add_argument(
        "--frontier-cast",
        action="store_true",
        help=(
            "Scope participant-wide model and voice checks to the six default "
            "frontier talkshow characters instead of every stored participant profile."
        ),
    )
    args = parser.parse_args()

    load_env_file(Path(args.env_file))
    api_base = args.api_base.rstrip("/")
    result: dict[str, Any] = {
        "schema_version": "live_provider_smoke_evidence.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "api_base": api_base,
        "participant_id": args.participant_id,
    }

    try:
        with httpx.Client(timeout=60) as client:
            readiness = get_json(client, f"{api_base}/api/v1/system/live-provider-readiness")
            result["live_provider_readiness"] = readiness_summary(readiness)

            participants = get_json(client, f"{api_base}/api/v1/participant-profiles")
            participant = find_record(participants, args.participant_id, "participant profile")
            result["participant"] = {
                "id": participant.get("id"),
                "display_name": participant.get("display_name"),
                "model_endpoint_id": participant.get("model_endpoint_id"),
                "model_id": participant.get("model_id"),
                "voice_profile_id": participant.get("voice_profile_id"),
            }

            model_endpoints = get_json(client, f"{api_base}/api/v1/model-endpoints")
            voice_endpoints = get_json(client, f"{api_base}/api/v1/voicebox-endpoints")
            voice_profiles = get_json(client, f"{api_base}/api/v1/voice-profiles")

            participant_scope_ids = participant_voice_scope_ids(
                parse_participant_ids(args.participant_ids),
                frontier_cast=frontier_cast_scope_enabled(args),
            )
            if participant_scope_ids:
                result["participant_scope"] = {
                    "schema_version": "participant_scope.v1",
                    "scope": "frontier_cast" if frontier_cast_scope_enabled(args) else "explicit",
                    "participant_ids": participant_scope_ids,
                }

            if not args.skip_openrouter and args.all_participant_models:
                result["model_participants"] = run_all_participant_model_smokes(
                    participants=filter_participants(participants, participant_scope_ids),
                    model_endpoints=model_endpoints,
                )
                result["model_summary"] = participant_smoke_summary(
                    result["model_participants"],
                    profile_id_key="model_id",
                    schema_version="model_participant_smoke_summary.v1",
                )
            elif not args.skip_openrouter:
                model_endpoint = find_record(
                    model_endpoints,
                    str(participant.get("model_endpoint_id") or ""),
                    "model endpoint",
                )
                result["openrouter"] = run_openrouter_smoke(
                    model_endpoint=model_endpoint,
                    model_id=str(participant.get("model_id") or ""),
                    participant_name=str(participant.get("display_name") or args.participant_id),
                )

            if not args.skip_voice and args.all_participant_voices:
                result["voicebox_participant_scope"] = {
                    "schema_version": "voicebox_participant_scope.v1",
                    "scope": (
                        "frontier_cast"
                        if frontier_cast_scope_enabled(args)
                        else "explicit_or_all"
                    ),
                    "participant_ids": participant_scope_ids,
                }
                result["voicebox_participants"] = run_all_participant_voice_smokes(
                    participants=filter_participants(
                        participants,
                        participant_scope_ids,
                    ),
                    voice_profiles=voice_profiles,
                    voice_endpoints=voice_endpoints,
                    text=args.text,
                    output_path=Path(args.output),
                )
                result["voicebox_summary"] = voicebox_participant_summary(
                    result["voicebox_participants"]
                )
            elif not args.skip_voice:
                voice_profile = find_record(
                    voice_profiles,
                    str(participant.get("voice_profile_id") or ""),
                    "voice profile",
                )
                voice_endpoint = find_record(
                    voice_endpoints,
                    str(voice_profile.get("voicebox_endpoint_id") or ""),
                    "voicebox endpoint",
                )
                result["voicebox"] = run_voice_smoke(
                    endpoint=voice_endpoint,
                    profile=voice_profile,
                    text=args.text,
                    output_path=Path(args.output),
                )
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        emit_result(result, args, stderr=True)
        return 1

    blocking_sections = []
    if not args.skip_openrouter and args.all_participant_models:
        failed_model_count = (
            result.get("model_summary", {}).get("failed_count")
            if isinstance(result.get("model_summary"), dict)
            else 0
        )
        if failed_model_count:
            blocking_sections.append("openrouter")
    elif not args.skip_openrouter and result.get("openrouter", {}).get("status") != "pass":
        blocking_sections.append("openrouter")
    if not args.skip_voice and args.all_participant_voices:
        failed_voice_count = (
            result.get("voicebox_summary", {}).get("failed_count")
            if isinstance(result.get("voicebox_summary"), dict)
            else 0
        )
        if failed_voice_count:
            blocking_sections.append("voicebox")
    elif not args.skip_voice and result.get("voicebox", {}).get("status") != "pass":
        blocking_sections.append("voicebox")
    result["status"] = "fail" if blocking_sections else "pass"
    result["blocking_sections"] = blocking_sections
    if args.requirements_output and "voicebox" in blocking_sections:
        result["requirements_update"] = append_voicebox_requirements(
            Path(args.requirements_output),
            result,
        )
    emit_result(result, args)
    return 1 if blocking_sections else 0


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_json(client: httpx.Client, url: str) -> Any:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def find_record(records: Any, record_id: str, label: str) -> dict[str, Any]:
    if not isinstance(records, list):
        raise ValueError(f"{label} list response was not a list")
    for record in records:
        if isinstance(record, dict) and record.get("id") == record_id:
            return record
    raise ValueError(f"{label} {record_id or '<empty>'} was not found")


def parse_participant_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def frontier_cast_scope_enabled(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "frontier_cast", False)
        or getattr(args, "frontier_cast_voices", False)
    )


def participant_voice_scope_ids(
    participant_ids: list[str],
    *,
    frontier_cast: bool = False,
) -> list[str]:
    if participant_ids:
        return participant_ids
    if frontier_cast:
        return list(FRONTIER_CAST_PARTICIPANT_IDS)
    return []


def filter_participants(participants: Any, participant_ids: list[str]) -> list[dict[str, Any]]:
    if not isinstance(participants, list):
        raise ValueError("participant profile response was not a list")
    records = [
        participant
        for participant in participants
        if isinstance(participant, dict)
    ]
    if not participant_ids:
        return records
    by_id = {
        str(participant.get("id") or ""): participant
        for participant in records
    }
    missing = [participant_id for participant_id in participant_ids if participant_id not in by_id]
    if missing:
        raise ValueError(f"participant profile(s) not found: {', '.join(missing)}")
    return [by_id[participant_id] for participant_id in participant_ids]


def readiness_summary(readiness: dict[str, Any]) -> dict[str, Any]:
    voicebox = next(
        (
            check
            for check in readiness.get("checks", [])
            if isinstance(check, dict) and check.get("category") == "voicebox"
        ),
        {},
    )
    comfyui = next(
        (
            check
            for check in readiness.get("checks", [])
            if isinstance(check, dict) and check.get("category") == "comfyui"
        ),
        {},
    )
    unhealthy = (
        comfyui.get("details", {}).get("unhealthy_endpoints", [])
        if isinstance(comfyui.get("details"), dict)
        else []
    )
    admission = None
    if unhealthy and isinstance(unhealthy[0], dict):
        admission = unhealthy[0].get("prompt_admission")
    return {
        "status": readiness.get("status"),
        "blockers": readiness.get("blockers", []),
        "warnings": readiness.get("warnings", []),
        "voicebox_status": voicebox.get("status"),
        "voicebox_unhealthy_endpoints": (
            voicebox.get("details", {}).get("unhealthy_endpoints", [])
            if isinstance(voicebox.get("details"), dict)
            else []
        ),
        "comfyui_status": comfyui.get("status"),
        "comfyui_prompt_admission": admission,
    }


def run_openrouter_smoke(
    *,
    model_endpoint: dict[str, Any],
    model_id: str,
    participant_name: str,
) -> dict[str, Any]:
    if model_endpoint.get("id") != "openrouter":
        raise ValueError("selected participant is not assigned to the openrouter endpoint")
    token = resolve_env_reference(str(model_endpoint.get("credential_reference") or ""))
    if not token:
        raise ValueError("OpenRouter credential reference did not resolve")
    base_url = str(model_endpoint.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ValueError("OpenRouter endpoint has no base_url")
    if not model_id:
        raise ValueError("selected participant has no model_id")
    capabilities = model_endpoint.get("capabilities") if isinstance(model_endpoint, dict) else {}
    site_url = ""
    app_title = ""
    if isinstance(capabilities, dict):
        site_url = str(capabilities.get("site_url") or "")
        app_title = str(capabilities.get("app_title") or "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if site_url:
        headers["HTTP-Referer"] = site_url
    if app_title:
        headers["X-Title"] = app_title
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "system",
                "content": "Reply with one concise German sentence for a live smoke test.",
            },
            {
                "role": "user",
                "content": f"Sprich als {participant_name} in einem DialectiCore Pilottest.",
            },
        ],
        "temperature": 0.2,
        "max_tokens": 80,
    }
    with httpx.Client(timeout=60) as client:
        response = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
    body = response.json()
    content = (
        body.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(body, dict)
        else ""
    )
    return {
        "status": "pass" if content else "fail",
        "endpoint_id": model_endpoint.get("id"),
        "model_id": model_id,
        "response_chars": len(str(content or "")),
        "sample": str(content or "")[:180],
    }


def run_all_participant_model_smokes(
    *,
    participants: Any,
    model_endpoints: Any,
) -> list[dict[str, Any]]:
    if not isinstance(participants, list):
        raise ValueError("participant profile response was not a list")
    results: list[dict[str, Any]] = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        endpoint_id = str(participant.get("model_endpoint_id") or "")
        model_id = str(participant.get("model_id") or "")
        model_endpoint: dict[str, Any] | None = None
        try:
            model_endpoint = find_record(model_endpoints, endpoint_id, "model endpoint")
            smoke = run_openrouter_smoke(
                model_endpoint=model_endpoint,
                model_id=model_id,
                participant_name=str(
                    participant.get("display_name") or participant.get("id") or ""
                ),
            )
        except Exception as exc:
            smoke = {
                "status": "fail",
                "endpoint_id": endpoint_id or None,
                "model_id": model_id or None,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "action": "fix_model_endpoint_or_participant_model_configuration",
            }
            if model_endpoint is not None:
                smoke["endpoint_name"] = model_endpoint.get("name")
        results.append(
            {
                **smoke,
                "participant_id": participant.get("id"),
                "participant_name": participant.get("display_name"),
            }
        )
    return results


def run_voice_smoke(
    *,
    endpoint: dict[str, Any],
    profile: dict[str, Any],
    text: str,
    output_path: Path,
) -> dict[str, Any]:
    base_url = str(endpoint.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ValueError("Voicebox endpoint has no base_url")
    capabilities = endpoint.get("capabilities") if isinstance(endpoint, dict) else {}
    path = "/generate/stream"
    accept = "audio/wav"
    engine = "chatterbox"
    normalize = False
    effects_chain: list[Any] = []
    if isinstance(capabilities, dict):
        path = str(capabilities.get("stream_generation_path") or path)
        accept = str(capabilities.get("accept") or accept)
        engine = str(capabilities.get("default_engine") or engine)
        if isinstance(capabilities.get("normalize_default"), bool):
            normalize = bool(capabilities["normalize_default"])
        if isinstance(capabilities.get("effects_chain_default"), list):
            effects_chain = list(capabilities["effects_chain_default"])
    payload = {
        "profile_id": profile.get("voice_id"),
        "text": text,
        "language": profile.get("language") or "de",
        "engine": engine,
        "normalize": normalize,
        "effects_chain": effects_chain,
    }
    if not payload["profile_id"]:
        raise ValueError("voice profile has no voice_id")
    headers = {"Accept": accept, "Content-Type": "application/json"}
    token = resolve_env_reference(str(endpoint.get("credential_reference") or ""))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=90, verify=voicebox_endpoint_verify(endpoint)) as client:
        response = client.post(f"{base_url}{path}", headers=headers, json=payload)
    canary = voicebox_response_evidence(
        response=response,
        endpoint=endpoint,
        profile=profile,
        payload=payload,
        path=path,
    )
    if response.status_code >= 400:
        return {
            **canary,
            "status": "fail",
            "action": "fix_voicebox_generation_then_rerun_health_check",
        }
    if not response.content:
        return {
            **canary,
            "status": "fail",
            "action": "fix_voicebox_generation_then_rerun_health_check",
            "failure_reason": "empty_audio",
        }
    if not canary["riff_wave"]:
        return {
            **canary,
            "status": "fail",
            "action": "fix_voicebox_generation_then_rerun_health_check",
            "failure_reason": "not_riff_wave",
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)
    return {
        **canary,
        "status": "pass",
        "output": str(output_path),
    }


def run_all_participant_voice_smokes(
    *,
    participants: Any,
    voice_profiles: Any,
    voice_endpoints: Any,
    text: str,
    output_path: Path,
) -> list[dict[str, Any]]:
    if not isinstance(participants, list):
        raise ValueError("participant profile response was not a list")
    results: list[dict[str, Any]] = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        voice_profile_id = str(participant.get("voice_profile_id") or "")
        if not voice_profile_id:
            results.append(
                {
                    "status": "fail",
                    "participant_id": participant.get("id"),
                    "participant_name": participant.get("display_name"),
                    "failure_reason": "missing_voice_profile",
                    "action": "assign_voice_profile",
                }
            )
            continue
        participant_id = str(participant.get("id") or voice_profile_id or "participant")
        voice_profile: dict[str, Any] | None = None
        voice_endpoint: dict[str, Any] | None = None
        try:
            voice_profile = find_record(voice_profiles, voice_profile_id, "voice profile")
            voice_endpoint = find_record(
                voice_endpoints,
                str(voice_profile.get("voicebox_endpoint_id") or ""),
                "voicebox endpoint",
            )
            voice_output_path = participant_voice_output_path(output_path, participant_id)
            smoke = run_voice_smoke(
                endpoint=voice_endpoint,
                profile=voice_profile,
                text=text,
                output_path=voice_output_path,
            )
        except Exception as exc:
            smoke = {
                "schema_version": "voicebox_stream_smoke_evidence.v1",
                "status": "fail",
                "voice_profile_id": voice_profile_id,
                "voice_name": voice_profile.get("name") if voice_profile else None,
                "profile_id": voice_profile.get("voice_id") if voice_profile else None,
                "endpoint_id": voice_endpoint.get("id") if voice_endpoint else None,
                "endpoint_name": voice_endpoint.get("name") if voice_endpoint else None,
                "url": voice_endpoint_generation_url(voice_endpoint),
                "failure_reason": "voice_smoke_exception",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "action": "fix_voice_profile_or_voicebox_endpoint_configuration",
            }
        results.append(
            {
                **smoke,
                "participant_id": participant.get("id"),
                "participant_name": participant.get("display_name"),
                "model_endpoint_id": participant.get("model_endpoint_id"),
                "model_id": participant.get("model_id"),
            }
        )
    return results


def participant_voice_output_path(base_output_path: Path, participant_id: str) -> Path:
    safe_id = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-"
        for char in participant_id.strip().lower()
    ).strip("-")
    safe_id = safe_id or "participant"
    suffix = base_output_path.suffix or ".wav"
    return base_output_path.with_name(f"{base_output_path.stem}-{safe_id}{suffix}")


def voicebox_participant_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return participant_smoke_summary(
        results,
        profile_id_key="voice_profile_id",
        schema_version="voicebox_participant_smoke_summary.v1",
    )


def participant_smoke_summary(
    results: list[dict[str, Any]],
    *,
    profile_id_key: str,
    schema_version: str,
) -> dict[str, Any]:
    pass_count = sum(1 for result in results if result.get("status") == "pass")
    failed = [result for result in results if result.get("status") != "pass"]
    return {
        "schema_version": schema_version,
        "participant_count": len(results),
        "pass_count": pass_count,
        "failed_count": len(failed),
        "failed_participant_ids": [
            result.get("participant_id")
            for result in failed
            if isinstance(result.get("participant_id"), str)
        ][:20],
        f"failed_{profile_id_key}s": [
            result.get(profile_id_key)
            for result in failed
            if isinstance(result.get(profile_id_key), str)
        ][:20],
    }


def voicebox_endpoint_verify(endpoint: dict[str, Any]) -> bool | str:
    capabilities = endpoint.get("capabilities") if isinstance(endpoint, dict) else {}
    ca_path = ""
    if isinstance(capabilities, dict):
        ca_path = str(capabilities.get("tls_ca_cert_path") or "").strip()
    return ca_path if ca_path else True


def voice_endpoint_generation_url(endpoint: dict[str, Any] | None) -> str | None:
    if not isinstance(endpoint, dict):
        return None
    base_url = str(endpoint.get("base_url") or "").rstrip("/")
    if not base_url:
        return None
    capabilities = endpoint.get("capabilities") if isinstance(endpoint, dict) else {}
    path = "/generate/stream"
    if isinstance(capabilities, dict):
        path = str(capabilities.get("stream_generation_path") or path)
    return f"{base_url}{path}"


def voicebox_response_evidence(
    *,
    response: httpx.Response,
    endpoint: dict[str, Any],
    profile: dict[str, Any],
    payload: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    content = response.content or b""
    return {
        "schema_version": "voicebox_stream_smoke_evidence.v1",
        "endpoint_id": endpoint.get("id"),
        "endpoint_name": endpoint.get("name"),
        "adapter_type": endpoint.get("adapter_type"),
        "url": f"{str(endpoint.get('base_url') or '').rstrip('/')}{path}",
        "voice_profile_id": profile.get("id"),
        "voice_name": profile.get("name"),
        "profile_id": payload.get("profile_id"),
        "language": payload.get("language"),
        "engine": payload.get("engine"),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "bytes": len(content),
        "riff_wave": content[:12].startswith(b"RIFF") and content[8:12] == b"WAVE",
    }


def emit_result(result: dict[str, Any], args: argparse.Namespace, stderr: bool = False) -> None:
    output = {"result": result}
    if args.evidence_output:
        output["evidence_file"] = write_evidence(Path(args.evidence_output), result)
    print(json.dumps(output, indent=2, sort_keys=True), file=sys.stderr if stderr else sys.stdout)


def write_evidence(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(payload)
    return {
        "path": str(path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def append_voicebox_requirements(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    voicebox = result.get("voicebox") if isinstance(result.get("voicebox"), dict) else {}
    voicebox_participants = (
        result.get("voicebox_participants")
        if isinstance(result.get("voicebox_participants"), list)
        else []
    )
    failed_participants = [
        item
        for item in voicebox_participants
        if isinstance(item, dict) and item.get("status") != "pass"
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        f"### Voicebox Smoke Recheck Added {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "DialectiCore live provider smoke still cannot generate B1 speech.",
        "",
        "- script: `scripts/live_provider_smoke.py`",
        f"- participant_id: `{result.get('participant_id')}`",
    ]
    if failed_participants:
        lines.extend(
            [
                f"- participant voice checks: `{len(voicebox_participants)}`",
                f"- failed participant voices: `{len(failed_participants)}`",
                "",
                "Failed participant voice samples:",
            ]
        )
        for item in failed_participants[:12]:
            lines.append(
                "- "
                f"participant_id=`{item.get('participant_id')}`; "
                f"voice_profile_id=`{item.get('voice_profile_id')}`; "
                f"endpoint_id=`{item.get('endpoint_id')}`; "
                f"profile_id=`{item.get('profile_id')}`; "
                f"engine=`{item.get('engine')}`; "
                f"status=`{item.get('status')}`; "
                f"HTTP=`{item.get('status_code')}`; "
                f"content_type=`{item.get('content_type')}`; "
                f"bytes=`{item.get('bytes')}`; "
                f"riff_wave=`{item.get('riff_wave')}`; "
                f"action=`{item.get('action')}`"
            )
    else:
        lines.extend(
            [
                f"- endpoint_id: `{voicebox.get('endpoint_id')}`",
                f"- endpoint_name: `{voicebox.get('endpoint_name')}`",
                f"- URL: `{voicebox.get('url')}`",
                f"- profile_id: `{voicebox.get('profile_id')}`",
                f"- voice_profile_id: `{voicebox.get('voice_profile_id')}`",
                f"- engine: `{voicebox.get('engine')}`",
                f"- status: `{voicebox.get('status')}`",
                f"- HTTP status: `{voicebox.get('status_code')}`",
                f"- content type: `{voicebox.get('content_type')}`",
                f"- bytes: `{voicebox.get('bytes')}`",
                f"- RIFF/WAVE detected: `{voicebox.get('riff_wave')}`",
                f"- required action: `{voicebox.get('action')}`",
            ]
        )
    lines.extend(
        [
            "",
            "Acceptance for the B1-side fix: the same request must return HTTP 200",
            "with `Accept: audio/wav`, a non-empty RIFF/WAVE payload for every",
            "configured participant voice, and the DialectiCore live-provider",
            "readiness Voicebox category must return `pass`.",
            "",
        ]
    )
    section = "\n".join(lines)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(section)
    return {"path": str(path), "appended": True}


def resolve_env_reference(reference: str) -> str | None:
    if not reference:
        return None
    if not reference.startswith("env:"):
        raise ValueError(f"unsupported credential reference scheme in {reference.split(':', 1)[0]}")
    return os.getenv(reference.split(":", 1)[1])


if __name__ == "__main__":
    raise SystemExit(main())
