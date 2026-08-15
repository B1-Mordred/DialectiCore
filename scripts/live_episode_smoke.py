#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_EVIDENCE_OUTPUT = "output/smoke/live-episode-smoke-evidence.json"
DEFAULT_REQUIREMENTS_OUTPUT = "/home/mordred/media-requirements.md"
DEFAULT_MODERATOR_ID = "claude"
DEFAULT_PARTICIPANTS = ["claude", "chatgpt", "deepseek", "grok", "gemini", "mistral"]
DEFAULT_SYNTHETIC_MODERATOR_ID = "host"
DEFAULT_SYNTHETIC_PARTICIPANTS = ["host", "optimist", "skeptic", "practitioner"]
SYNTHETIC_MOCK_ENDPOINTS = (
    ("voicebox", "voicebox-endpoints", "mock-voicebox"),
    ("comfyui", "comfyui-endpoints", "mock-comfyui"),
)
ARTIFACT_OUTPUT_FILENAMES = {
    "final_render": "final-render.mp4",
    "export_package": "youtube-export-package.zip",
    "production_manifest": "production-manifest.json",
}
APPROVAL_COMMENTS = {
    "research_review": "Approve live smoke evidence pack for source-bound workflow test.",
    "transcript_review": "Approve live smoke transcript for media workflow test.",
    "preview_render_review": "Approve preview render for live smoke final render/package test.",
    "final_render_review": "Approve final render for live smoke package and dry-run publish test.",
}
DEFAULT_MANUAL_RESEARCH_SOURCE = "\n".join(
    [
        "This operator brief is the allowed source material for the frontier-model pilot.",
        "The episode should compare practical usefulness for a small production team.",
        "Discuss strengths, weaknesses, cost-benefit tradeoffs, reliability, and deployment fit.",
        "ChatGPT represents a broad OpenAI-style generalist with strong product integration.",
        "Claude represents careful synthesis, moderation, and risk-aware reasoning.",
        "DeepSeek represents cost-effective technical analysis and implementation focus.",
        "Grok represents fast contrarian challenge and direct rebuttal.",
        "Gemini represents efficient multimodal and product-suite synthesis.",
        "Mistral represents concise European and open-model pragmatism.",
        (
            "Treat numeric benchmark, price, market-share, and release-status statements "
            "as unsupported unless a separate approved source is added."
        ),
    ]
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or continue a short real episode smoke through normal workflow gates."
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--episode-id", default="")
    parser.add_argument("--title", default="Live Smoke Pilot: Frontier KI")
    parser.add_argument("--user-id", default="live-smoke")
    parser.add_argument("--max-advances", type=int, default=6)
    parser.add_argument(
        "--production-target",
        choices=["audio_first", "native_visual"],
        default="audio_first",
        help="Episode completion target to declare for newly created smoke episodes.",
    )
    parser.add_argument("--target-duration-minutes", type=int, default=2)
    parser.add_argument("--permitted-deviation-percent", type=int, default=50)
    parser.add_argument(
        "--participant-ids",
        default=",".join(DEFAULT_PARTICIPANTS),
        help=(
            "Comma-separated participant profile IDs for newly created smoke episodes. "
            "Defaults to the full six-character frontier cast."
        ),
    )
    parser.add_argument(
        "--moderator-id",
        default=DEFAULT_MODERATOR_ID,
        help="Participant profile ID to assign the moderator role.",
    )
    parser.add_argument(
        "--research-mode",
        choices=["none", "manual"],
        default="none",
        help="Create a no-research pilot or build a manual evidence pack before discussion.",
    )
    parser.add_argument(
        "--evidence-only",
        action="store_true",
        help=(
            "With --episode-id, only refresh readiness, production report, package, "
            "and artifact download evidence. Does not start, approve, or advance workflow."
        ),
    )
    parser.add_argument(
        "--manual-source-title",
        default="Frontier model live smoke evidence brief",
    )
    parser.add_argument(
        "--manual-source-uri",
        default="dialecticore://smoke/frontier-model-evidence-brief",
    )
    parser.add_argument(
        "--manual-source-content",
        default=DEFAULT_MANUAL_RESEARCH_SOURCE,
    )
    parser.add_argument("--no-auto-approve", action="store_true")
    parser.add_argument(
        "--evidence-output",
        default=DEFAULT_EVIDENCE_OUTPUT,
        help="Where to write the stable JSON smoke evidence.",
    )
    parser.add_argument(
        "--no-evidence-file",
        action="store_true",
        help="Print the result only; do not write a JSON evidence file.",
    )
    parser.add_argument(
        "--cleanup-start-only-run",
        action="store_true",
        help=(
            "When --max-advances=0 created this episode, cancel the durable run after "
            "recording start/replay/readiness evidence so live readiness is not left blocked."
        ),
    )
    parser.add_argument(
        "--artifact-output-dir",
        default="",
        help=(
            "Optional directory where downloadable final render, export package, "
            "and production manifest artifacts are saved for inspection."
        ),
    )
    parser.add_argument(
        "--requirements-output",
        default=DEFAULT_REQUIREMENTS_OUTPUT,
        help=(
            "When production evidence shows B1 media/provider failures, append a "
            "Codex-readable B1-side repair handoff here."
        ),
    )
    parser.add_argument(
        "--no-requirements-update",
        action="store_true",
        help="Do not append B1-side repair requirements even when provider failures are found.",
    )
    parser.add_argument(
        "--ignore-native-visual-preflight-blockers",
        action="store_true",
        help=(
            "Continue a native_visual smoke even when episode pilot readiness already "
            "reports native visual blockers."
        ),
    )
    parser.add_argument(
        "--no-refresh-native-visual-health",
        action="store_true",
        help="Do not refresh ComfyUI endpoint health before native_visual preflight.",
    )
    parser.add_argument(
        "--wait-native-visual-admission-seconds",
        type=int,
        default=0,
        help=(
            "For native_visual smokes, keep refreshing ComfyUI health and pilot "
            "readiness for this many seconds before failing preflight."
        ),
    )
    parser.add_argument(
        "--wait-native-visual-admission-interval-seconds",
        type=int,
        default=30,
        help="Polling interval used with --wait-native-visual-admission-seconds.",
    )
    parser.add_argument(
        "--provider-smoke-preflight",
        action="store_true",
        help=(
            "Before starting production, run real model and Voicebox smokes for the "
            "selected cast and stop cleanly if any selected participant fails."
        ),
    )
    parser.add_argument(
        "--provider-smoke-output",
        default="output/smoke/live-episode-provider-voice.wav",
        help="Base WAV path used for per-participant provider preflight voice samples.",
    )
    parser.add_argument(
        "--provider-env-file",
        default=".env",
        help="Env file to load before provider smoke preflight credential resolution.",
    )
    parser.add_argument(
        "--cleanup-preflight-draft",
        action="store_true",
        help=(
            "When this command created a draft episode and provider preflight blocks "
            "before workflow start, cancel that draft episode and record cleanup evidence."
        ),
    )
    parser.add_argument(
        "--synthetic-mock-mode",
        action="store_true",
        help=(
            "Run a full deterministic API workflow using the seeded mock cast and "
            "temporarily enabled mock Voicebox/ComfyUI endpoints. Restores endpoint "
            "records, participant records, and workflow endpoint routing before exiting."
        ),
    )
    args = parser.parse_args()
    if args.evidence_only and not args.episode_id:
        parser.error("--evidence-only requires --episode-id")
    if args.provider_smoke_preflight and not args.evidence_only:
        from scripts.live_provider_smoke import load_env_file

        load_env_file(Path(args.provider_env_file))
    participant_ids = parse_participant_ids(args.participant_ids)
    moderator_id = args.moderator_id
    if args.synthetic_mock_mode:
        if participant_ids == DEFAULT_PARTICIPANTS:
            participant_ids = DEFAULT_SYNTHETIC_PARTICIPANTS
        if moderator_id == DEFAULT_MODERATOR_ID:
            moderator_id = DEFAULT_SYNTHETIC_MODERATOR_ID

    result: dict[str, Any] = {
        "schema_version": "live_episode_smoke_evidence.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "invocation": smoke_invocation_summary(sys.argv),
        "api_base": args.api_base.rstrip("/"),
        "requested_episode_id": args.episode_id or None,
        "requested_production_target": args.production_target,
        "requested_target_duration_minutes": args.target_duration_minutes,
        "requested_permitted_deviation_percent": args.permitted_deviation_percent,
        "requested_participant_ids": participant_ids,
        "requested_moderator_id": moderator_id,
        "research_mode": args.research_mode,
        "evidence_only": bool(args.evidence_only),
        "synthetic_mock_mode": bool(args.synthetic_mock_mode),
        "max_advances": args.max_advances,
        "auto_approve": not args.no_auto_approve,
        "created_episode": False,
        "advance_results": [],
        "run_until_blocked_results": [],
        "approval_results": [],
    }
    synthetic_endpoint_restore: list[dict[str, Any]] = []
    synthetic_workflow_restore: list[dict[str, Any]] = []
    synthetic_participant_restore: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=300) as client:
            if args.synthetic_mock_mode and not args.evidence_only:
                log_progress("enabling synthetic mock media endpoints")
                synthetic_endpoint_restore = enable_synthetic_mock_endpoints(
                    client,
                    result["api_base"],
                )
                result["synthetic_mock_endpoint_setup"] = synthetic_endpoint_setup_summary(
                    synthetic_endpoint_restore
                )
                log_progress("pointing synthetic ComfyUI workflows at mock endpoint")
                synthetic_workflow_restore = enable_synthetic_mock_workflows(
                    client,
                    result["api_base"],
                )
                result["synthetic_mock_workflow_setup"] = synthetic_workflow_setup_summary(
                    synthetic_workflow_restore
                )
                log_progress("enabling synthetic mock participant profiles")
                synthetic_participant_restore = enable_synthetic_mock_participants(
                    client,
                    result["api_base"],
                    result["requested_participant_ids"],
                )
                result["synthetic_mock_participant_setup"] = (
                    synthetic_participant_setup_summary(synthetic_participant_restore)
                )
            if args.episode_id:
                log_progress(f"loading existing episode {args.episode_id}")
                episode = get_episode(client, result["api_base"], args.episode_id)
            else:
                log_progress(f"creating episode {args.title!r}")
                episode = create_episode(
                    client,
                    result["api_base"],
                    args.title,
                    production_target=args.production_target,
                    target_duration_minutes=args.target_duration_minutes,
                    permitted_deviation_percent=args.permitted_deviation_percent,
                    participant_ids=result["requested_participant_ids"],
                    moderator_id=moderator_id,
                    research_enabled=args.research_mode != "none",
                )
                result["created_episode"] = True
                if args.research_mode == "manual":
                    log_progress(f"building manual research pack for {episode['id']}")
                    episode = build_research(
                        client,
                        result["api_base"],
                        episode["id"],
                        user_id=args.user_id,
                        title=args.manual_source_title,
                        uri=args.manual_source_uri,
                        content=args.manual_source_content,
                    )
                    result["manual_research_built"] = True
            result["episode_id"] = episode["id"]
            log_progress(f"checking cast readiness for {episode['id']}")
            cast_readiness = get_cast_readiness_summary(
                client,
                result["api_base"],
                episode,
                fallback_participant_ids=result["requested_participant_ids"],
                fallback_moderator_id=moderator_id,
            )
            result["cast_readiness"] = cast_readiness
            if cast_readiness.get("status") != "pass":
                result["episode"] = episode_summary(episode)
                result["status"] = "cast_preflight_blocked"
                restore_synthetic_mock_state(
                    client,
                    result["api_base"],
                    result,
                    endpoint_restore=synthetic_endpoint_restore,
                    workflow_restore=synthetic_workflow_restore,
                    participant_restore=synthetic_participant_restore,
                )
                synthetic_endpoint_restore = []
                synthetic_workflow_restore = []
                synthetic_participant_restore = []
                output = result_output(result, args)
                print(json.dumps(output, indent=2, sort_keys=True))
                return 2
            if not args.evidence_only and args.provider_smoke_preflight:
                log_progress(f"running provider smoke preflight for {episode['id']}")
                provider_preflight = provider_smoke_preflight_summary(
                    client,
                    result["api_base"],
                    participant_ids=selected_cast_participant_ids(cast_readiness),
                    voice_output_path=Path(args.provider_smoke_output),
                )
                result["provider_smoke_preflight"] = provider_preflight
                if provider_preflight.get("status") != "pass":
                    provider_preflight_requirements = (
                        append_provider_preflight_requirements(
                            Path(args.requirements_output),
                            result,
                            provider_preflight,
                            disabled=args.no_requirements_update,
                        )
                    )
                    if provider_preflight_requirements.get("appended") is True:
                        result["requirements_update"] = provider_preflight_requirements
                    if should_cleanup_provider_preflight_draft(args, result, episode):
                        log_progress(
                            f"cleaning up provider-preflight draft for {episode['id']}"
                        )
                        cleanup = cleanup_provider_preflight_draft(
                            client,
                            result["api_base"],
                            episode["id"],
                            user_id=args.user_id,
                        )
                        result["preflight_cleanup"] = cleanup
                        episode = get_episode(client, result["api_base"], episode["id"])
                    result["episode"] = episode_summary(episode)
                    result["status"] = "provider_preflight_blocked"
                    restore_synthetic_mock_state(
                        client,
                        result["api_base"],
                        result,
                        endpoint_restore=synthetic_endpoint_restore,
                        workflow_restore=synthetic_workflow_restore,
                        participant_restore=synthetic_participant_restore,
                    )
                    synthetic_endpoint_restore = []
                    synthetic_workflow_restore = []
                    synthetic_participant_restore = []
                    output = result_output(result, args)
                    print(json.dumps(output, indent=2, sort_keys=True))
                    return 2
            if not args.evidence_only and should_refresh_native_visual_health(args):
                log_progress("refreshing native ComfyUI health before preflight")
                result["comfyui_health_refresh"] = refresh_comfyui_health(
                    client,
                    result["api_base"],
                )
            log_progress(f"checking pilot readiness for {episode['id']}")
            preflight_pilot = get_json(
                client,
                f"{result['api_base']}/api/v1/episodes/{episode['id']}/pilot-readiness",
            )
            preflight_summary = native_visual_preflight_summary(preflight_pilot)
            result["initial_pilot_readiness"] = pilot_readiness_summary(preflight_pilot)
            result["initial_live_provider_readiness"] = get_live_provider_readiness_summary(
                client,
                result["api_base"],
            )
            if (
                not args.evidence_only
                and should_wait_for_native_visual_admission(
                    args,
                    preflight_pilot,
                    preflight_summary,
                )
            ):
                wait_result = wait_for_native_visual_admission(
                    client,
                    result["api_base"],
                    episode["id"],
                    max_wait_seconds=args.wait_native_visual_admission_seconds,
                    interval_seconds=args.wait_native_visual_admission_interval_seconds,
                    refresh_health=not args.no_refresh_native_visual_health,
                )
                result["native_visual_admission_wait"] = wait_result
                final_pilot = wait_result.get("final_pilot")
                if isinstance(final_pilot, dict):
                    preflight_pilot = final_pilot
                    preflight_summary = native_visual_preflight_summary(preflight_pilot)
            if not args.evidence_only and native_visual_preflight_should_stop(
                args=args,
                pilot=preflight_pilot,
                preflight_summary=preflight_summary,
            ):
                result["episode"] = episode_summary(episode)
                result["status"] = "preflight_blocked"
                result["preflight_blockers"] = preflight_summary.get("blockers", [])
                result["final_pilot_readiness"] = pilot_readiness_summary(preflight_pilot)
                result["live_provider_readiness"] = get_live_provider_readiness_summary(
                    client,
                    result["api_base"],
                )
                result["production_test_report"] = get_json(
                    client,
                    f"{result['api_base']}/api/v1/episodes/{episode['id']}/production-test-report",
                )
                result["production_test_report"] = (
                    append_provider_requirements_and_refresh_report(
                        result,
                        result["production_test_report"],
                        live_provider_readiness=result["live_provider_readiness"],
                        requirements_output=Path(args.requirements_output),
                        disabled=args.no_requirements_update,
                    )
                )
                result["production_test_summary"] = production_test_summary(
                    result["production_test_report"]
                )
                restore_synthetic_mock_state(
                    client,
                    result["api_base"],
                    result,
                    endpoint_restore=synthetic_endpoint_restore,
                    workflow_restore=synthetic_workflow_restore,
                    participant_restore=synthetic_participant_restore,
                )
                synthetic_endpoint_restore = []
                synthetic_workflow_restore = []
                synthetic_participant_restore = []
                output = result_output(result, args)
                print(json.dumps(output, indent=2, sort_keys=True))
                return 2

            if args.evidence_only:
                log_progress(
                    f"evidence-only mode for {episode['id']}; skipping workflow changes"
                )
            else:
                log_progress(f"starting durable workflow run for {episode['id']}")
                workflow_start = ensure_workflow_started(
                    client,
                    result["api_base"],
                    episode["id"],
                    user_id=args.user_id,
                )
                result["workflow_start"] = workflow_start
                episode = get_episode(client, result["api_base"], episode["id"])

                for _ in range(max(args.max_advances, 0)):
                    if not args.no_auto_approve:
                        log_progress(f"approving pending gates for {episode['id']}")
                        approvals = approve_pending_gates(
                            client,
                            result["api_base"],
                            episode,
                            user_id=args.user_id,
                        )
                        result["approval_results"].extend(approvals)
                        if approvals:
                            episode = get_episode(client, result["api_base"], episode["id"])
                    elif pending_approval_count(episode) > 0:
                        log_progress(
                            f"workflow for {episode['id']} is awaiting approval; "
                            "auto-approval is disabled"
                        )
                        break

                    if episode.get("status") == "COMPLETED":
                        break

                    log_progress(
                        f"running workflow until review for {episode['id']} "
                        f"(attempt {len(result['advance_results']) + 1}/"
                        f"{max(args.max_advances, 0)})"
                    )
                    run_result = run_episode_until_blocked(
                        client,
                        result["api_base"],
                        episode["id"],
                        user_id=args.user_id,
                    )
                    run_summary = run_until_blocked_summary(run_result)
                    result["run_until_blocked_results"].append(run_summary)
                    result["advance_results"].extend(
                        advance_summary({"episode": run_result.get("episode"), "summary": summary})
                        for summary in run_result.get("summaries", [])
                        if isinstance(summary, dict)
                    )
                    episode = run_result["episode"]
                    if (
                        args.no_auto_approve
                        and run_result.get("stop_reason") == "pending_approval"
                    ):
                        break

                if not args.no_auto_approve:
                    log_progress(f"approving final pending gates for {episode['id']}")
                    approvals = approve_pending_gates(
                        client,
                        result["api_base"],
                        episode,
                        user_id=args.user_id,
                    )
                    result["approval_results"].extend(approvals)
                    if approvals:
                        episode = get_episode(client, result["api_base"], episode["id"])

            log_progress(f"checking completion readiness for {episode['id']}")
            completion = get_json(
                client,
                f"{result['api_base']}/api/v1/episodes/{episode['id']}/workflow/completion-readiness",
            )
            log_progress(f"checking final pilot readiness for {episode['id']}")
            pilot = get_json(
                client,
                f"{result['api_base']}/api/v1/episodes/{episode['id']}/pilot-readiness",
            )
            log_progress("checking live provider readiness")
            live_provider_readiness = get_live_provider_readiness_summary(
                client,
                result["api_base"],
            )
            log_progress(f"loading production test report for {episode['id']}")
            production_test_report = get_json(
                client,
                f"{result['api_base']}/api/v1/episodes/{episode['id']}/production-test-report",
            )
            log_progress(f"inspecting delivery package for {episode['id']}")
            package_inspection = inspect_delivery_package(
                client,
                result["api_base"],
                episode["id"],
                production_test_report,
            )
            artifact_output_dir = (
                Path(args.artifact_output_dir) if args.artifact_output_dir else None
            )
            log_progress(f"checking artifact downloads for {episode['id']}")
            artifact_download_checks = artifact_download_evidence(
                client,
                result["api_base"],
                production_test_report,
                artifact_output_dir,
            )
            if (
                synthetic_endpoint_restore
                or synthetic_workflow_restore
                or synthetic_participant_restore
            ):
                log_progress("restoring synthetic mock state")
                restore_synthetic_mock_state(
                    client,
                    result["api_base"],
                    result,
                    endpoint_restore=synthetic_endpoint_restore,
                    workflow_restore=synthetic_workflow_restore,
                    participant_restore=synthetic_participant_restore,
                )
                synthetic_endpoint_restore = []
                synthetic_workflow_restore = []
                synthetic_participant_restore = []
            if should_cleanup_start_only_run(args, result, episode):
                log_progress(f"cleaning up start-only smoke run for {episode['id']}")
                cleanup = cleanup_started_workflow(
                    client,
                    result["api_base"],
                    episode["id"],
                    user_id=args.user_id,
                )
                result["cleanup"] = cleanup
                episode = get_episode(client, result["api_base"], episode["id"])
                log_progress("checking live provider readiness after cleanup")
                result["post_cleanup_live_provider_readiness"] = (
                    get_live_provider_readiness_summary(
                        client,
                        result["api_base"],
                    )
                )
    except Exception as exc:
        if (
            synthetic_endpoint_restore
            or synthetic_workflow_restore
            or synthetic_participant_restore
        ):
            log_progress("restoring synthetic mock state after error")
            try:
                with httpx.Client(timeout=300) as restore_client:
                    restore_synthetic_mock_state(
                        restore_client,
                        result["api_base"],
                        result,
                        endpoint_restore=synthetic_endpoint_restore,
                        workflow_restore=synthetic_workflow_restore,
                        participant_restore=synthetic_participant_restore,
                    )
            except Exception as restore_exc:
                result["synthetic_mock_restore_error"] = {
                    "schema_version": "synthetic_mock_restore_error.v1",
                    "status": "fail",
                    "error": f"{type(restore_exc).__name__}: {restore_exc}",
                }
        if should_cleanup_start_only_run_after_error(args, result):
            log_progress(
                "cleaning up start-only smoke run after error for "
                f"{result.get('episode_id')}"
            )
            result["cleanup"] = cleanup_started_workflow_after_error(
                result["api_base"],
                str(result["episode_id"]),
                user_id=args.user_id,
            )
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        output = result_output(result, args)
        print(json.dumps(output, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    result["episode"] = episode_summary(episode)
    result["discussion_speaker_coverage"] = discussion_speaker_coverage_summary(
        episode,
        cast_readiness=result.get("cast_readiness"),
    )
    result["completion_readiness"] = {
        "status": completion.get("status"),
        "production_target": completion.get("production_target"),
        "production_target_satisfied": completion.get("production_target_satisfied"),
        "failed_checks": completion.get("failed_checks", []),
        "visual_source_summary": completion.get("visual_source_summary", {}),
    }
    pilot_summary = pilot_readiness_summary(pilot)
    result["pilot_readiness"] = pilot_summary
    result["final_pilot_readiness"] = pilot_summary
    result["live_provider_readiness"] = live_provider_readiness
    production_test_report = append_provider_requirements_and_refresh_report(
        result,
        production_test_report,
        live_provider_readiness=live_provider_readiness,
        requirements_output=Path(args.requirements_output),
        disabled=args.no_requirements_update,
    )
    result["production_test_report"] = production_test_report
    result["production_test_summary"] = production_test_summary(production_test_report)
    result["package_inspection"] = package_inspection
    result["artifact_download_checks"] = artifact_download_checks
    result["production_acceptance_summary"] = production_acceptance_summary(
        episode=episode,
        completion=completion,
        production_test_report=production_test_report,
        package_inspection=package_inspection,
        artifact_download_checks=artifact_download_checks,
        discussion_speaker_coverage=result["discussion_speaker_coverage"],
    )
    if start_only_smoke_succeeded(args, result):
        result["status"] = "start_only_pass"
    elif episode.get("status") != "COMPLETED":
        result["status"] = "incomplete"
    elif completion.get("status") != "pass":
        result["status"] = "fail"
    elif production_test_report.get("status") != "pass":
        result["status"] = "fail"
    elif package_inspection.get("status") not in {"pass", "skipped"}:
        result["status"] = "fail"
    elif artifact_download_checks.get("status") != "pass":
        result["status"] = "fail"
    elif result["discussion_speaker_coverage"].get("status") != "pass":
        result["status"] = "fail"
    else:
        result["status"] = "pass"
    output = result_output(result, args)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if result["status"] in {"pass", "start_only_pass"} else 2


def log_progress(message: str) -> None:
    timestamp = datetime.now(UTC).isoformat()
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def result_output(result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    output = {"result": result}
    if not args.no_evidence_file:
        output["evidence_file"] = write_evidence(Path(args.evidence_output), result)
    return output


def write_evidence(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(payload)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def smoke_invocation_summary(argv: list[str]) -> dict[str, Any]:
    script = Path(argv[0]).name if argv else "live_episode_smoke.py"
    args = list(argv[1:])
    redacted_args: list[str] = []
    redact_next = False
    for value in args:
        if redact_next:
            redacted_args.append("<redacted>")
            redact_next = False
            continue
        if value in {"--api-key", "--b1-api-key", "--openrouter-api-key"}:
            redacted_args.append(value)
            redact_next = True
            continue
        if any(
            value.startswith(prefix)
            for prefix in (
                "--api-key=",
                "--b1-api-key=",
                "--openrouter-api-key=",
            )
        ):
            key, _, _raw = value.partition("=")
            redacted_args.append(f"{key}=<redacted>")
            continue
        redacted_args.append(value)
    return {
        "schema_version": "live_episode_smoke_invocation.v1",
        "script": script,
        "argv": [script, *redacted_args],
        "args": redacted_args,
        "rerun_command": shlex.join(
            ["python", "scripts/live_episode_smoke.py", *redacted_args]
        ),
    }


def should_cleanup_start_only_run(
    args: argparse.Namespace,
    result: dict[str, Any],
    episode: dict[str, Any],
) -> bool:
    if not getattr(args, "cleanup_start_only_run", False):
        return False
    if int(getattr(args, "max_advances", 0) or 0) != 0:
        return False
    if result.get("created_episode") is not True:
        return False
    workflow_start = result.get("workflow_start")
    if not isinstance(workflow_start, dict) or workflow_start.get("status") != "started":
        return False
    run = episode_workflow_run(episode)
    return run.get("state") == "running" and episode.get("status") not in {
        "COMPLETED",
        "CANCELLED",
    }


def should_cleanup_start_only_run_after_error(
    args: argparse.Namespace,
    result: dict[str, Any],
) -> bool:
    if not getattr(args, "cleanup_start_only_run", False):
        return False
    if int(getattr(args, "max_advances", 0) or 0) != 0:
        return False
    if result.get("created_episode") is not True:
        return False
    if not result.get("episode_id"):
        return False
    workflow_start = result.get("workflow_start")
    return isinstance(workflow_start, dict) and workflow_start.get("status") == "started"


def cleanup_started_workflow_after_error(
    api_base: str,
    episode_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=300) as client:
            return cleanup_started_workflow(
                client,
                api_base,
                episode_id,
                user_id=user_id,
            )
    except Exception as exc:
        return {
            "schema_version": "live_smoke_start_only_cleanup.v1",
            "status": "failed",
            "episode_id": episode_id,
            "error": f"{type(exc).__name__}: {exc}",
        }


def cleanup_started_workflow(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/episodes/{episode_id}/workflow/actions",
        json={
            "action": "cancel",
            "user_id": user_id,
            "comment": "Cancel start-only live smoke run after durable-start evidence capture.",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("workflow cleanup response was not an object")
    run = episode_workflow_run(payload)
    return {
        "schema_version": "live_smoke_start_only_cleanup.v1",
        "status": "cancelled" if payload.get("status") == "CANCELLED" else "unexpected",
        "episode_id": payload.get("id"),
        "episode_status": payload.get("status"),
        "run_id": run.get("run_id"),
        "run_state": run.get("state"),
        "cancelled": payload.get("workflow_control", {}).get("cancelled") is True
        if isinstance(payload.get("workflow_control"), dict)
        else False,
    }


def should_cleanup_provider_preflight_draft(
    args: argparse.Namespace,
    result: dict[str, Any],
    episode: dict[str, Any],
) -> bool:
    if not getattr(args, "cleanup_preflight_draft", False):
        return False
    if result.get("created_episode") is not True:
        return False
    if result.get("workflow_start") is not None:
        return False
    if episode.get("status") != "DRAFT":
        return False
    if episode_workflow_run(episode):
        return False
    return True


def cleanup_provider_preflight_draft(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/episodes/{episode_id}/workflow/actions",
        json={
            "action": "cancel",
            "user_id": user_id,
            "comment": (
                "Cancel provider-preflight draft smoke episode after blocked provider check."
            ),
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("provider preflight cleanup response was not an object")
    return {
        "schema_version": "live_smoke_provider_preflight_cleanup.v1",
        "status": "cancelled" if payload.get("status") == "CANCELLED" else "unexpected",
        "episode_id": payload.get("id"),
        "episode_status": payload.get("status"),
        "workflow_started": bool(episode_workflow_run(payload)),
        "cancelled": payload.get("workflow_control", {}).get("cancelled") is True
        if isinstance(payload.get("workflow_control"), dict)
        else False,
    }


def start_only_smoke_succeeded(
    args: argparse.Namespace,
    result: dict[str, Any],
) -> bool:
    if int(getattr(args, "max_advances", 0) or 0) != 0:
        return False
    workflow_start = result.get("workflow_start")
    if not isinstance(workflow_start, dict):
        return False
    replay = workflow_start.get("replay")
    if not isinstance(replay, dict) or replay.get("status") != "pass":
        return False
    if workflow_start.get("status") not in {"started", "already_running"}:
        return False
    if getattr(args, "cleanup_start_only_run", False):
        cleanup = result.get("cleanup")
        return isinstance(cleanup, dict) and cleanup.get("status") == "cancelled"
    return True


def parse_participant_ids(value: str) -> list[str]:
    participant_ids = [item.strip() for item in value.split(",") if item.strip()]
    if not participant_ids:
        raise ValueError("at least one participant ID is required")
    seen: set[str] = set()
    duplicates: list[str] = []
    for participant_id in participant_ids:
        if participant_id in seen and participant_id not in duplicates:
            duplicates.append(participant_id)
        seen.add(participant_id)
    if duplicates:
        raise ValueError(f"duplicate participant ID(s): {', '.join(duplicates)}")
    return participant_ids


def build_episode_participant_entries(
    participant_ids: list[str],
    *,
    moderator_id: str,
) -> list[dict[str, str]]:
    moderator = moderator_id.strip()
    if not moderator:
        raise ValueError("moderator ID is required")
    if moderator not in participant_ids:
        raise ValueError("moderator ID must be included in participant IDs")
    return [
        {"participant_profile_id": moderator, "role": "moderator"},
        *[
            {"participant_profile_id": participant_id, "role": "panelist"}
            for participant_id in participant_ids
            if participant_id != moderator
        ],
    ]


def enable_synthetic_mock_endpoints(
    client: httpx.Client,
    api_base: str,
) -> list[dict[str, Any]]:
    restore_records: list[dict[str, Any]] = []
    for scope, route, endpoint_id in SYNTHETIC_MOCK_ENDPOINTS:
        endpoint = get_json(client, f"{api_base}/api/v1/{route}/{endpoint_id}")
        if not isinstance(endpoint, dict):
            raise ValueError(f"{endpoint_id} endpoint response was not an object")
        restore_records.append({"scope": scope, "route": route, "endpoint": endpoint})
        if endpoint.get("enabled") is True and endpoint.get("health_status") == "healthy":
            continue
        updated = {**endpoint, "enabled": True, "health_status": "healthy"}
        response = client.put(
            f"{api_base}/api/v1/{route}/{endpoint_id}",
            json=endpoint_update_payload(updated),
        )
        response.raise_for_status()
    return restore_records


def restore_synthetic_mock_endpoints(
    client: httpx.Client,
    api_base: str,
    restore_records: list[dict[str, Any]],
) -> dict[str, Any]:
    restored = []
    failures = []
    for record in restore_records:
        endpoint = record.get("endpoint")
        route = record.get("route")
        if not isinstance(endpoint, dict) or not route:
            failures.append({"endpoint_id": None, "error": "invalid_restore_record"})
            continue
        endpoint_id = str(endpoint.get("id") or "")
        try:
            response = client.put(
                f"{api_base}/api/v1/{route}/{endpoint_id}",
                json=endpoint_update_payload(endpoint),
            )
            response.raise_for_status()
            payload = response.json()
            restored.append(
                {
                    "scope": record.get("scope"),
                    "endpoint_id": endpoint_id,
                    "enabled": payload.get("enabled") if isinstance(payload, dict) else None,
                    "health_status": (
                        payload.get("health_status") if isinstance(payload, dict) else None
                    ),
                }
            )
        except Exception as exc:  # pragma: no cover - surfaced in smoke evidence
            failures.append(
                {
                    "scope": record.get("scope"),
                    "endpoint_id": endpoint_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "schema_version": "synthetic_mock_endpoint_restore.v1",
        "status": "pass" if not failures else "fail",
        "restored": restored,
        "failures": failures,
    }


def enable_synthetic_mock_participants(
    client: httpx.Client,
    api_base: str,
    participant_ids: list[str],
) -> list[dict[str, Any]]:
    restore_records: list[dict[str, Any]] = []
    for participant_id in participant_ids:
        profile = get_json(client, f"{api_base}/api/v1/participant-profiles/{participant_id}")
        if not isinstance(profile, dict):
            raise ValueError(f"{participant_id} participant profile response was not an object")
        restore_records.append({"participant": profile})
        if profile.get("enabled") is True:
            continue
        updated = {**profile, "enabled": True}
        response = client.put(
            f"{api_base}/api/v1/participant-profiles/{participant_id}",
            json=participant_update_payload(updated),
        )
        response.raise_for_status()
    return restore_records


def restore_synthetic_mock_participants(
    client: httpx.Client,
    api_base: str,
    restore_records: list[dict[str, Any]],
) -> dict[str, Any]:
    restored = []
    failures = []
    for record in restore_records:
        participant = record.get("participant")
        if not isinstance(participant, dict):
            failures.append({"participant_id": None, "error": "invalid_restore_record"})
            continue
        participant_id = str(participant.get("id") or "")
        try:
            response = client.put(
                f"{api_base}/api/v1/participant-profiles/{participant_id}",
                json=participant_update_payload(participant),
            )
            response.raise_for_status()
            payload = response.json()
            restored.append(
                {
                    "participant_id": participant_id,
                    "enabled": payload.get("enabled") if isinstance(payload, dict) else None,
                }
            )
        except Exception as exc:  # pragma: no cover - surfaced in smoke evidence
            failures.append(
                {
                    "participant_id": participant_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "schema_version": "synthetic_mock_participant_restore.v1",
        "status": "pass" if not failures else "fail",
        "restored": restored,
        "failures": failures,
    }


def enable_synthetic_mock_workflows(
    client: httpx.Client,
    api_base: str,
) -> list[dict[str, Any]]:
    response = client.get(f"{api_base}/api/v1/comfyui-workflows")
    response.raise_for_status()
    workflows = response.json()
    if not isinstance(workflows, list):
        raise ValueError("comfyui workflow list response was not an array")
    restore_records: list[dict[str, Any]] = []
    for workflow in workflows:
        if not isinstance(workflow, dict) or workflow.get("enabled") is not True:
            continue
        restore_records.append({"workflow": workflow})
        if workflow.get("comfyui_endpoint_id") == "mock-comfyui":
            continue
        updated = {**workflow, "comfyui_endpoint_id": "mock-comfyui"}
        response = client.put(
            f"{api_base}/api/v1/comfyui-workflows/{workflow.get('id')}",
            json=workflow_update_payload(updated),
        )
        response.raise_for_status()
    return restore_records


def restore_synthetic_mock_workflows(
    client: httpx.Client,
    api_base: str,
    restore_records: list[dict[str, Any]],
) -> dict[str, Any]:
    restored = []
    failures = []
    for record in restore_records:
        workflow = record.get("workflow")
        if not isinstance(workflow, dict):
            failures.append({"workflow_id": None, "error": "invalid_restore_record"})
            continue
        workflow_id = str(workflow.get("id") or "")
        try:
            response = client.put(
                f"{api_base}/api/v1/comfyui-workflows/{workflow_id}",
                json=workflow_update_payload(workflow),
            )
            response.raise_for_status()
            payload = response.json()
            restored.append(
                {
                    "workflow_id": workflow_id,
                    "comfyui_endpoint_id": (
                        payload.get("comfyui_endpoint_id")
                        if isinstance(payload, dict)
                        else None
                    ),
                    "enabled": payload.get("enabled") if isinstance(payload, dict) else None,
                }
            )
        except Exception as exc:  # pragma: no cover - surfaced in smoke evidence
            failures.append(
                {
                    "workflow_id": workflow_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "schema_version": "synthetic_mock_workflow_restore.v1",
        "status": "pass" if not failures else "fail",
        "restored": restored,
        "failures": failures,
    }


def restore_synthetic_mock_state(
    client: httpx.Client,
    api_base: str,
    result: dict[str, Any],
    *,
    endpoint_restore: list[dict[str, Any]],
    workflow_restore: list[dict[str, Any]],
    participant_restore: list[dict[str, Any]],
) -> None:
    if participant_restore:
        result["synthetic_mock_participant_restore"] = restore_synthetic_mock_participants(
            client,
            api_base,
            participant_restore,
        )
    if workflow_restore:
        result["synthetic_mock_workflow_restore"] = restore_synthetic_mock_workflows(
            client,
            api_base,
            workflow_restore,
        )
    if endpoint_restore:
        result["synthetic_mock_endpoint_restore"] = restore_synthetic_mock_endpoints(
            client,
            api_base,
            endpoint_restore,
        )


def synthetic_endpoint_setup_summary(
    restore_records: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoints = []
    for record in restore_records:
        endpoint = record.get("endpoint")
        if not isinstance(endpoint, dict):
            continue
        endpoints.append(
            {
                "scope": record.get("scope"),
                "endpoint_id": endpoint.get("id"),
                "previous_enabled": endpoint.get("enabled"),
                "previous_health_status": endpoint.get("health_status"),
            }
        )
    return {
        "schema_version": "synthetic_mock_endpoint_setup.v1",
        "status": "pass",
        "endpoints": endpoints,
    }


def synthetic_workflow_setup_summary(
    restore_records: list[dict[str, Any]],
) -> dict[str, Any]:
    workflows = []
    for record in restore_records:
        workflow = record.get("workflow")
        if not isinstance(workflow, dict):
            continue
        workflows.append(
            {
                "workflow_id": workflow.get("id"),
                "previous_comfyui_endpoint_id": workflow.get("comfyui_endpoint_id"),
                "workflow_type": workflow.get("workflow_type"),
                "enabled": workflow.get("enabled"),
            }
        )
    return {
        "schema_version": "synthetic_mock_workflow_setup.v1",
        "status": "pass",
        "workflows": workflows,
    }


def synthetic_participant_setup_summary(
    restore_records: list[dict[str, Any]],
) -> dict[str, Any]:
    participants = []
    for record in restore_records:
        participant = record.get("participant")
        if not isinstance(participant, dict):
            continue
        participants.append(
            {
                "participant_id": participant.get("id"),
                "previous_enabled": participant.get("enabled"),
                "model_endpoint_id": participant.get("model_endpoint_id"),
                "voice_profile_id": participant.get("voice_profile_id"),
                "visual_profile_id": participant.get("visual_profile_id"),
            }
        )
    return {
        "schema_version": "synthetic_mock_participant_setup.v1",
        "status": "pass",
        "participants": participants,
    }


def endpoint_update_payload(endpoint: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "name",
        "adapter_type",
        "base_url",
        "credential_reference",
        "default_timeout_seconds",
        "max_concurrency",
        "retry_policy",
        "enabled",
        "capabilities",
        "health_status",
    ]
    return {key: endpoint[key] for key in keys if key in endpoint}


def participant_update_payload(participant: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "name",
        "display_name",
        "participant_type",
        "model_endpoint_id",
        "model_id",
        "system_prompt_template",
        "perspective",
        "expertise",
        "speaking_style",
        "sampling_settings",
        "tool_policy_id",
        "voice_profile_id",
        "visual_profile_id",
        "enabled",
    ]
    return {key: participant[key] for key in keys if key in participant}


def workflow_update_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "name",
        "workflow_type",
        "version",
        "comfyui_endpoint_id",
        "output_asset_type",
        "api_workflow",
        "prompt_template",
        "default_parameters",
        "enabled",
    ]
    return {key: workflow[key] for key in keys if key in workflow}


def create_episode(
    client: httpx.Client,
    api_base: str,
    title: str,
    *,
    production_target: str,
    target_duration_minutes: int,
    permitted_deviation_percent: int,
    participant_ids: list[str],
    moderator_id: str,
    research_enabled: bool,
) -> dict[str, Any]:
    participant_entries = build_episode_participant_entries(
        participant_ids,
        moderator_id=moderator_id,
    )
    payload = {
        "definition": {
            "title": title,
            "topic": {
                "central_question": (
                    "Welche Frontier KI liefert heute den besten praktischen Nutzen fuer ein "
                    "kleines Produktionsteam?"
                ),
                "scope": ["praktischer Nutzen", "Kosten", "Zuverlaessigkeit"],
                "required_dimensions": ["Nutzen", "Kosten", "Risiken"],
                "exclusions": ["keine unbelegten Produktversprechen"],
            },
            "format": {
                "show_format_id": "frontier_panel_v1",
                "target_duration_minutes": target_duration_minutes,
                "permitted_deviation_percent": permitted_deviation_percent,
                "participant_count": len(participant_entries),
                "host_control": "high",
                "allow_interruptions": False,
                "allow_follow_up_questions": True,
                "maximum_monologue_seconds": 10,
                "discussion_intensity": "medium",
            },
            "languages": {
                "source_language": "de",
                "outputs": [{"language": "de", "mode": "canonical"}],
            },
            "participants": participant_entries,
            "research": {
                "enabled": research_enabled,
                "depth": "standard",
                "require_source_links": research_enabled,
                "approval_required": research_enabled,
            },
            "media": {
                "aspect_ratio": "16:9",
                "width": 1280,
                "height": 720,
                "fps": 24,
                "visual_style": "studio_realistic",
                "camera_style": "multi_camera",
                "subtitle_mode": "selectable",
                "generate_broll": False,
                "generate_citation_cards": False,
            },
            "workflow": {
                "mode": "transcript_review",
                "production_target": production_target,
                "retry_failed_assets": True,
                "maximum_stage_retries": 1,
            },
            "quality": {
                "block_on_unsupported_high_impact_claims": False,
                "block_on_missing_audio": True,
                "block_on_sync_error_ms": 240,
                "block_on_missing_subtitles": False,
            },
        }
    }
    response = client.post(f"{api_base}/api/v1/episodes", json=payload)
    response.raise_for_status()
    return response.json()


def build_research(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    *,
    user_id: str,
    title: str,
    uri: str,
    content: str,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/episodes/{episode_id}/research/build",
        json={
            "user_id": user_id,
            "sources": [
                {
                    "title": title,
                    "uri": uri,
                    "source_type": "manual_source",
                    "content": content,
                }
            ],
            "require_approval": True,
        },
    )
    response.raise_for_status()
    return response.json()


def get_episode(client: httpx.Client, api_base: str, episode_id: str) -> dict[str, Any]:
    return get_json(client, f"{api_base}/api/v1/episodes/{episode_id}")


def get_json_list(client: httpx.Client, url: str) -> list[dict[str, Any]]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"expected list response from {url}")
    return [item for item in payload if isinstance(item, dict)]


def get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"expected object response from {url}")
    return payload


def ensure_workflow_started(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/episodes/{episode_id}/workflow/start",
        json={
            "user_id": user_id,
            "comment": "Live smoke started durable production workflow before advancing.",
        },
    )
    status_code = response.status_code
    already_active = False
    if status_code == 422:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        already_active = "already active" in str(detail).lower()
        if not already_active:
            response.raise_for_status()
        episode = get_episode(client, api_base, episode_id)
    else:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("workflow start response was not an object")
        episode = payload
    replay = workflow_replay_summary(client, api_base, episode_id)
    run = episode_workflow_run(episode)
    return {
        "schema_version": "live_smoke_workflow_start.v1",
        "status": "already_running" if already_active else "started",
        "response_status_code": status_code,
        "run_id": run.get("run_id"),
        "run_state": run.get("state"),
        "current_stage": run.get("current_stage"),
        "started_by": run.get("started_by"),
        "discussion_session_present": isinstance(episode.get("discussion_session"), dict),
        "replay": replay,
    }


def workflow_replay_summary(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
) -> dict[str, Any]:
    replay = get_json(client, f"{api_base}/api/v1/episodes/{episode_id}/workflow/replay")
    replayed = replay.get("replayed") if isinstance(replay.get("replayed"), dict) else {}
    current = replay.get("current") if isinstance(replay.get("current"), dict) else {}
    return {
        "schema_version": "live_smoke_workflow_replay_summary.v1",
        "status": replay.get("status"),
        "event_count": replay.get("event_count"),
        "event_log_checksum": replay.get("event_log_checksum"),
        "replayed_state": replayed.get("state"),
        "replayed_stage": replayed.get("current_stage"),
        "current_state": current.get("state"),
        "current_stage": current.get("current_stage"),
        "issues": replay.get("issues", []),
    }


def episode_workflow_run(episode: dict[str, Any]) -> dict[str, Any]:
    control = episode.get("workflow_control")
    if not isinstance(control, dict):
        return {}
    run = control.get("run")
    return run if isinstance(run, dict) else {}


def provider_smoke_preflight_summary(
    client: httpx.Client,
    api_base: str,
    *,
    participant_ids: list[str],
    voice_output_path: Path,
) -> dict[str, Any]:
    from scripts.live_provider_smoke import (
        filter_participants,
        participant_smoke_summary,
        run_all_participant_model_smokes,
        run_all_participant_voice_smokes,
        voicebox_participant_summary,
    )

    try:
        participants = get_json_list(client, f"{api_base}/api/v1/participant-profiles")
        model_endpoints = get_json_list(client, f"{api_base}/api/v1/model-endpoints")
        voice_profiles = get_json_list(client, f"{api_base}/api/v1/voice-profiles")
        voice_endpoints = get_json_list(client, f"{api_base}/api/v1/voicebox-endpoints")
        selected_participants = filter_participants(participants, participant_ids)
        model_results = run_all_participant_model_smokes(
            participants=selected_participants,
            model_endpoints=model_endpoints,
        )
        voice_results = run_all_participant_voice_smokes(
            participants=selected_participants,
            voice_profiles=voice_profiles,
            voice_endpoints=voice_endpoints,
            text="Guten Tag. DialectiCore prueft die Besetzung vor dem Produktionsstart.",
            output_path=voice_output_path,
        )
    except Exception as exc:
        return {
            "schema_version": "live_episode_provider_smoke_preflight.v1",
            "status": "fail",
            "participant_ids": participant_ids,
            "reason": f"{type(exc).__name__}: {exc}",
            "blockers": ["provider_smoke_preflight_unavailable"],
        }
    model_summary = participant_smoke_summary(
        model_results,
        profile_id_key="model_id",
        schema_version="model_participant_smoke_summary.v1",
    )
    voice_summary = voicebox_participant_summary(voice_results)
    blockers: list[str] = []
    if model_summary["failed_count"]:
        blockers.append("one or more selected participant model smokes failed")
    if voice_summary["failed_count"]:
        blockers.append("one or more selected participant voice smokes failed")
    return {
        "schema_version": "live_episode_provider_smoke_preflight.v1",
        "status": "pass" if not blockers else "fail",
        "participant_ids": participant_ids,
        "model_participants": model_results,
        "model_summary": model_summary,
        "voicebox_participants": voice_results,
        "voicebox_summary": voice_summary,
        "blockers": blockers,
    }


def append_provider_preflight_requirements(
    path: Path,
    result: dict[str, Any],
    provider_preflight: dict[str, Any],
    *,
    disabled: bool = False,
) -> dict[str, Any]:
    if disabled:
        return {
            "path": str(path),
            "appended": False,
            "reason": "requirements_update_disabled",
        }
    voicebox_summary = (
        provider_preflight.get("voicebox_summary")
        if isinstance(provider_preflight.get("voicebox_summary"), dict)
        else {}
    )
    if not int(voicebox_summary.get("failed_count") or 0):
        return {
            "path": str(path),
            "appended": False,
            "reason": "no_voicebox_preflight_failure",
        }
    from scripts.live_provider_smoke import append_voicebox_requirements

    update = append_voicebox_requirements(
        path,
        {
            "participant_id": ",".join(provider_preflight.get("participant_ids", [])),
            "voicebox_participants": provider_preflight.get("voicebox_participants", []),
        },
    )
    return {
        **update,
        "source": "live_episode_provider_smoke_preflight",
        "failed_voice_count": voicebox_summary.get("failed_count"),
        "failed_participant_ids": voicebox_summary.get("failed_participant_ids", []),
    }


def get_cast_readiness_summary(
    client: httpx.Client,
    api_base: str,
    episode: dict[str, Any],
    *,
    fallback_participant_ids: list[str],
    fallback_moderator_id: str,
) -> dict[str, Any]:
    try:
        participants = get_json_list(client, f"{api_base}/api/v1/participant-profiles")
        model_endpoints = get_json_list(client, f"{api_base}/api/v1/model-endpoints")
        voice_profiles = get_json_list(client, f"{api_base}/api/v1/voice-profiles")
        visual_profiles = get_json_list(client, f"{api_base}/api/v1/visual-profiles")
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "schema_version": "cast_readiness_smoke_summary.v1",
            "status": "fail",
            "ready": False,
            "participant_count": 0,
            "configured_participant_count": 0,
            "issues": [f"cast_configuration_unavailable:{type(exc).__name__}"],
            "participants": [],
        }
    return cast_readiness_summary(
        episode,
        participants=participants,
        model_endpoints=model_endpoints,
        voice_profiles=voice_profiles,
        visual_profiles=visual_profiles,
        fallback_participant_ids=fallback_participant_ids,
        fallback_moderator_id=fallback_moderator_id,
    )


def cast_readiness_summary(
    episode: dict[str, Any],
    *,
    participants: list[dict[str, Any]],
    model_endpoints: list[dict[str, Any]],
    voice_profiles: list[dict[str, Any]],
    visual_profiles: list[dict[str, Any]],
    fallback_participant_ids: list[str],
    fallback_moderator_id: str,
) -> dict[str, Any]:
    cast = episode_cast_entries(
        episode,
        fallback_participant_ids=fallback_participant_ids,
        fallback_moderator_id=fallback_moderator_id,
    )
    participant_by_id = {str(item.get("id") or ""): item for item in participants}
    model_endpoint_by_id = {str(item.get("id") or ""): item for item in model_endpoints}
    voice_profile_by_id = {str(item.get("id") or ""): item for item in voice_profiles}
    visual_profile_by_id = {str(item.get("id") or ""): item for item in visual_profiles}
    participant_summaries = [
        cast_participant_readiness(
            cast_entry,
            participant=participant_by_id.get(cast_entry["participant_profile_id"]),
            model_endpoint_by_id=model_endpoint_by_id,
            voice_profile_by_id=voice_profile_by_id,
            visual_profile_by_id=visual_profile_by_id,
        )
        for cast_entry in cast
    ]
    issues = [
        f"{entry['participant_id']}:{issue}"
        for entry in participant_summaries
        for issue in entry.get("issues", [])
    ]
    moderator_count = sum(1 for entry in participant_summaries if entry.get("role") == "moderator")
    if moderator_count != 1:
        issues.append(f"cast:expected_one_moderator_found_{moderator_count}")
    return {
        "schema_version": "cast_readiness_smoke_summary.v1",
        "status": "pass" if not issues else "fail",
        "ready": not issues,
        "participant_count": len(cast),
        "configured_participant_count": sum(
            1 for entry in participant_summaries if entry.get("ready") is True
        ),
        "moderator_count": moderator_count,
        "issues": issues,
        "participants": participant_summaries,
    }


def episode_cast_entries(
    episode: dict[str, Any],
    *,
    fallback_participant_ids: list[str],
    fallback_moderator_id: str,
) -> list[dict[str, str]]:
    definition = episode.get("definition") if isinstance(episode.get("definition"), dict) else {}
    raw_cast = definition.get("participants") if isinstance(definition, dict) else None
    cast: list[dict[str, str]] = []
    if isinstance(raw_cast, list):
        for item in raw_cast:
            if not isinstance(item, dict):
                continue
            participant_id = str(item.get("participant_profile_id") or "").strip()
            if not participant_id:
                continue
            role = str(item.get("role") or "panelist").strip() or "panelist"
            cast.append({"participant_profile_id": participant_id, "role": role})
    if cast:
        return cast
    return build_episode_participant_entries(
        fallback_participant_ids,
        moderator_id=fallback_moderator_id,
    )


def cast_participant_readiness(
    cast_entry: dict[str, str],
    *,
    participant: dict[str, Any] | None,
    model_endpoint_by_id: dict[str, dict[str, Any]],
    voice_profile_by_id: dict[str, dict[str, Any]],
    visual_profile_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    participant_id = cast_entry["participant_profile_id"]
    issues: list[str] = []
    if participant is None:
        return {
            "participant_id": participant_id,
            "role": cast_entry["role"],
            "ready": False,
            "issues": ["participant_profile_missing"],
        }
    model_endpoint_id = str(participant.get("model_endpoint_id") or "")
    voice_profile_id = str(participant.get("voice_profile_id") or "")
    visual_profile_id = str(participant.get("visual_profile_id") or "")
    model_endpoint = model_endpoint_by_id.get(model_endpoint_id)
    voice_profile = voice_profile_by_id.get(voice_profile_id)
    visual_profile = visual_profile_by_id.get(visual_profile_id)
    if participant.get("enabled") is False:
        issues.append("participant_disabled")
    if not model_endpoint_id:
        issues.append("model_endpoint_missing")
    elif model_endpoint is None:
        issues.append("model_endpoint_unknown")
    elif model_endpoint.get("enabled") is False:
        issues.append("model_endpoint_disabled")
    if not str(participant.get("model_id") or ""):
        issues.append("model_id_missing")
    if not voice_profile_id:
        issues.append("voice_profile_missing")
    elif voice_profile is None:
        issues.append("voice_profile_unknown")
    elif voice_profile.get("enabled") is False:
        issues.append("voice_profile_disabled")
    if not visual_profile_id:
        issues.append("visual_profile_missing")
    elif visual_profile is None:
        issues.append("visual_profile_unknown")
    elif visual_profile.get("enabled") is False:
        issues.append("visual_profile_disabled")
    return {
        "participant_id": participant_id,
        "display_name": participant.get("display_name"),
        "role": cast_entry["role"],
        "participant_type": participant.get("participant_type"),
        "ready": not issues,
        "enabled": participant.get("enabled"),
        "model_endpoint_id": model_endpoint_id or None,
        "model_endpoint_enabled": model_endpoint.get("enabled") if model_endpoint else None,
        "model_endpoint_health_status": (
            model_endpoint.get("health_status") if model_endpoint else None
        ),
        "model_id": participant.get("model_id"),
        "voice_profile_id": voice_profile_id or None,
        "voice_profile_name": voice_profile.get("name") if voice_profile else None,
        "voice_profile_enabled": voice_profile.get("enabled") if voice_profile else None,
        "visual_profile_id": visual_profile_id or None,
        "visual_profile_name": visual_profile.get("name") if visual_profile else None,
        "visual_profile_enabled": visual_profile.get("enabled") if visual_profile else None,
        "primary_workflow_id": (
            visual_profile.get("primary_workflow_id") if visual_profile else None
        ),
        "issues": issues,
    }


def pilot_readiness_summary(pilot: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": pilot.get("status"),
        "production_target": pilot.get("production_target"),
        "target_status": pilot.get("target_status"),
        "selected_pilot_mode": pilot.get("selected_pilot_mode"),
        "pilot_modes": pilot.get("pilot_modes", []),
        "native_visual_preflight_summary": native_visual_preflight_summary(pilot),
        "blockers": pilot.get("blockers", []),
        "warnings": pilot.get("warnings", []),
        "all_stage_blockers": pilot.get("all_stage_blockers", []),
        "all_stage_warnings": pilot.get("all_stage_warnings", []),
    }


def get_live_provider_readiness_summary(
    client: httpx.Client,
    api_base: str,
) -> dict[str, Any]:
    try:
        readiness = get_json(client, f"{api_base}/api/v1/system/live-provider-readiness")
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "schema_version": "live_provider_readiness_smoke_summary.v1",
            "status": "fail",
            "reason": f"{type(exc).__name__}: {exc}",
            "checks": [],
            "blockers": ["live_provider_readiness_unavailable"],
            "warnings": [],
        }
    return live_provider_readiness_summary(readiness)


def live_provider_readiness_summary(readiness: dict[str, Any]) -> dict[str, Any]:
    checks = readiness.get("checks")
    check_list = checks if isinstance(checks, list) else []
    compact_checks = [
        compact_live_provider_check(check)
        for check in check_list
        if isinstance(check, dict) and live_provider_check_is_relevant(check)
    ]
    blockers: list[str] = []
    warnings: list[str] = []
    for check in compact_checks:
        blockers.extend(str(item) for item in check.get("blockers", []))
        warnings.extend(str(item) for item in check.get("warnings", []))
    return {
        "schema_version": "live_provider_readiness_smoke_summary.v1",
        "status": readiness.get("status"),
        "checked_at": readiness.get("checked_at"),
        "summary": readiness.get("summary", {}),
        "checks": compact_checks,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }


def live_provider_check_is_relevant(check: dict[str, Any]) -> bool:
    return str(check.get("category") or "") in {
        "voicebox",
        "comfyui",
        "managed_media_smoke",
        "workflow_orchestration",
        "production_runs",
        "publisher_targets",
        "publish_jobs",
    }


def compact_live_provider_check(check: dict[str, Any]) -> dict[str, Any]:
    details = check.get("details") if isinstance(check.get("details"), dict) else {}
    category = str(check.get("category") or "")
    compact: dict[str, Any] = {
        "category": category,
        "status": check.get("status"),
        "label": check.get("label"),
        "blockers": check.get("blockers", []),
        "warnings": check.get("warnings", []),
        "failed_readiness_checks": details.get("failed_readiness_checks", []),
    }
    if category in {"voicebox", "comfyui"}:
        compact.update(
            {
                "configured": details.get("configured"),
                "enabled": details.get("enabled"),
                "healthy": details.get("healthy"),
                "unhealthy": details.get("unhealthy"),
                "unknown": details.get("unknown"),
                "unhealthy_endpoints": [
                    compact_provider_endpoint(endpoint)
                    for endpoint in details.get("unhealthy_endpoints", [])
                    if isinstance(endpoint, dict)
                ],
                "unknown_health_endpoints": [
                    compact_provider_endpoint(endpoint)
                    for endpoint in details.get("unknown_health_endpoints", [])
                    if isinstance(endpoint, dict)
                ],
            }
        )
    elif category == "managed_media_smoke":
        compact.update(
            {
                "evidence_status": details.get("status"),
                "model": details.get("model"),
                "modality": details.get("modality"),
                "operation": details.get("operation"),
                "terminal_state": details.get("terminal_state"),
                "failure_category": details.get("failure_category"),
                "action": details.get("action"),
            }
        )
    elif category in {"workflow_orchestration", "production_runs"}:
        compact.update(
            {
                "attention_count": details.get("attention_count"),
                "current_blocked_production_handoff_count": details.get(
                    "current_blocked_production_handoff_count"
                ),
                "active_production_runs": details.get("active_production_runs"),
                "running_active_production_runs": details.get(
                    "running_active_production_runs"
                ),
                "failed_active_production_runs": details.get("failed_active_production_runs"),
                "cancelled_active_production_runs": details.get(
                    "cancelled_active_production_runs"
                ),
            }
        )
    return compact


def compact_provider_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    voice_generation = (
        endpoint.get("voice_generation")
        if isinstance(endpoint.get("voice_generation"), dict)
        else None
    )
    prompt_admission = (
        endpoint.get("prompt_admission")
        if isinstance(endpoint.get("prompt_admission"), dict)
        else None
    )
    result = {
        "id": endpoint.get("id"),
        "name": endpoint.get("name"),
        "adapter_type": endpoint.get("adapter_type"),
        "health_status": endpoint.get("health_status"),
    }
    if voice_generation is not None:
        result["voice_generation"] = {
            "ready": voice_generation.get("ready"),
            "status": voice_generation.get("status"),
            "status_code": voice_generation.get("status_code"),
            "content_type": voice_generation.get("content_type"),
            "bytes": voice_generation.get("bytes"),
            "riff_wave": voice_generation.get("riff_wave"),
            "engine": voice_generation.get("engine"),
            "action": voice_generation.get("action"),
        }
    if prompt_admission is not None:
        result["prompt_admission"] = {
            "ready": prompt_admission.get("ready"),
            "status_code": prompt_admission.get("status_code"),
            "code": prompt_admission.get("code"),
            "message": prompt_admission.get("message"),
        }
    return result


def should_refresh_native_visual_health(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "production_target", "") == "native_visual"
        and not getattr(args, "no_refresh_native_visual_health", False)
    )


def should_wait_for_native_visual_admission(
    args: argparse.Namespace,
    pilot: dict[str, Any],
    preflight_summary: dict[str, Any],
) -> bool:
    if getattr(args, "production_target", "") != "native_visual":
        return False
    if pilot.get("production_target") != "native_visual":
        return False
    if getattr(args, "ignore_native_visual_preflight_blockers", False):
        return False
    if int(getattr(args, "wait_native_visual_admission_seconds", 0) or 0) <= 0:
        return False
    return preflight_summary.get("status") != "pass"


def wait_for_native_visual_admission(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    *,
    max_wait_seconds: int,
    interval_seconds: int,
    refresh_health: bool = True,
) -> dict[str, Any]:
    started = monotonic()
    deadline = started + max(0, max_wait_seconds)
    interval = max(1, interval_seconds)
    attempts: list[dict[str, Any]] = []
    final_pilot: dict[str, Any] | None = None

    while True:
        refresh = refresh_comfyui_health(client, api_base) if refresh_health else None
        pilot = get_json(client, f"{api_base}/api/v1/episodes/{episode_id}/pilot-readiness")
        final_pilot = pilot
        summary = native_visual_preflight_summary(pilot)
        attempts.append(
            {
                "attempt_number": len(attempts) + 1,
                "elapsed_seconds": round(monotonic() - started, 3),
                "status": summary.get("status"),
                "prompt_admission_ready": summary.get("prompt_admission_ready"),
                "prompt_admission_blocked_endpoint_count": summary.get(
                    "prompt_admission_blocked_endpoint_count"
                ),
                "comfyui_health_refresh_status": (
                    refresh.get("status") if isinstance(refresh, dict) else None
                ),
            }
        )
        if summary.get("status") == "pass":
            return {
                "schema_version": "native_visual_admission_wait.v1",
                "status": "pass",
                "waited_seconds": round(monotonic() - started, 3),
                "attempt_count": len(attempts),
                "attempts": attempts,
                "final_pilot": final_pilot,
            }
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(interval, max(0.0, remaining)))

    return {
        "schema_version": "native_visual_admission_wait.v1",
        "status": "timeout",
        "waited_seconds": round(monotonic() - started, 3),
        "attempt_count": len(attempts),
        "attempts": attempts,
        "final_pilot": final_pilot,
    }


def refresh_comfyui_health(client: httpx.Client, api_base: str) -> dict[str, Any]:
    try:
        response = client.get(f"{api_base}/api/v1/comfyui-endpoints")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "schema_version": "live_smoke_comfyui_health_refresh.v1",
            "status": "fail",
            "refreshed": [],
            "issues": [f"list_comfyui_endpoints:{type(exc).__name__}"],
        }
    endpoints = payload if isinstance(payload, list) else []
    refreshable = [
        endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict) and should_refresh_comfyui_endpoint(endpoint)
    ]
    refreshed = [
        refresh_comfyui_endpoint_health(client, api_base, endpoint)
        for endpoint in refreshable
    ]
    issues = [
        f"{entry.get('endpoint_id')}:{entry.get('status')}"
        for entry in refreshed
        if entry.get("status") != "pass"
    ]
    return {
        "schema_version": "live_smoke_comfyui_health_refresh.v1",
        "status": "pass" if not issues else "warning",
        "candidate_endpoint_count": len(refreshable),
        "refreshed": refreshed,
        "issues": issues,
    }


def should_refresh_comfyui_endpoint(endpoint: dict[str, Any]) -> bool:
    if endpoint.get("enabled") is False:
        return False
    capabilities = (
        endpoint.get("capabilities")
        if isinstance(endpoint.get("capabilities"), dict)
        else {}
    )
    return endpoint.get("adapter_type") != "mock" or capabilities.get("native_comfyui") is True


def refresh_comfyui_endpoint_health(
    client: httpx.Client,
    api_base: str,
    endpoint: dict[str, Any],
) -> dict[str, Any]:
    endpoint_id = str(endpoint.get("id") or "")
    if not endpoint_id:
        return {"endpoint_id": None, "status": "fail", "reason": "missing_endpoint_id"}
    try:
        response = client.post(
            f"{api_base}/api/v1/comfyui-endpoints/{endpoint_id}/health"
        )
        if response.status_code >= 400:
            return {
                "endpoint_id": endpoint_id,
                "status": "fail",
                "response_status_code": response.status_code,
                "error": response.text[:500],
            }
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "endpoint_id": endpoint_id,
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "endpoint_id": endpoint_id,
            "status": "fail",
            "error": "health response was not an object",
        }
    capabilities = (
        payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    )
    return {
        "endpoint_id": endpoint_id,
        "status": "pass",
        "health_status": payload.get("health_status"),
        "native_comfyui": capabilities.get("native_comfyui"),
        "prompt_admission_ready": capabilities.get("prompt_admission_ready"),
        "prompt_admission": compact_endpoint_prompt_admission(capabilities),
    }


def compact_endpoint_prompt_admission(capabilities: dict[str, Any]) -> dict[str, Any] | None:
    probe = capabilities.get("prompt_admission_probe")
    if not isinstance(probe, dict):
        return None
    response = probe.get("response") if isinstance(probe.get("response"), dict) else {}
    detail = response.get("detail") if isinstance(response.get("detail"), dict) else {}
    hardware = (
        detail.get("hardware_resource_policy")
        if isinstance(detail.get("hardware_resource_policy"), dict)
        else {}
    )
    return {
        "ready": capabilities.get("prompt_admission_ready") is True,
        "status_code": probe.get("status_code"),
        "code": detail.get("code"),
        "message": detail.get("message"),
        "detail": hardware.get("detail"),
    }


def native_visual_preflight_should_stop(
    *,
    args: argparse.Namespace,
    pilot: dict[str, Any],
    preflight_summary: dict[str, Any],
) -> bool:
    if getattr(args, "ignore_native_visual_preflight_blockers", False):
        return False
    if getattr(args, "production_target", "") != "native_visual":
        return False
    if pilot.get("production_target") != "native_visual":
        return False
    return preflight_summary.get("status") != "pass"


def inspect_delivery_package(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    production_test_report: dict[str, Any],
) -> dict[str, Any]:
    package = (
        production_test_report.get("deliverables", {}).get("export_package", {})
        if isinstance(production_test_report.get("deliverables"), dict)
        else {}
    )
    package_asset_id = package.get("asset_id") if isinstance(package, dict) else None
    if not package_asset_id:
        return {
            "schema_version": "live_smoke_package_inspection.v1",
            "status": "skipped",
            "reason": "export_package_asset_missing",
        }
    response = client.get(
        f"{api_base}/api/v1/episodes/{episode_id}/youtube-package/inspect",
        params={"package_asset_id": package_asset_id},
    )
    if response.status_code >= 400:
        return {
            "schema_version": "live_smoke_package_inspection.v1",
            "status": "fail",
            "package_asset_id": package_asset_id,
            "response_status_code": response.status_code,
            "error": response.text[:500],
        }
    payload = response.json()
    if not isinstance(payload, dict):
        return {
            "schema_version": "live_smoke_package_inspection.v1",
            "status": "fail",
            "package_asset_id": package_asset_id,
            "error": "package inspection response was not an object",
        }
    return {
        "schema_version": "live_smoke_package_inspection.v1",
        "status": payload.get("status"),
        "package_asset_id": package_asset_id,
        "file_count": payload.get("file_count"),
        "manifest_schema_version": payload.get("manifest_schema_version"),
        "chapter_count": payload.get("chapter_count"),
        "subtitle_count": payload.get("subtitle_count"),
        "evidence_source_count": payload.get("evidence_source_count"),
        "manifest_matches_asset_metadata": payload.get("manifest_matches_asset_metadata"),
        "issues": payload.get("issues", []),
    }


def artifact_download_evidence(
    client: httpx.Client,
    api_base: str,
    production_test_report: dict[str, Any],
    artifact_output_dir: Path | None = None,
) -> dict[str, Any]:
    deliverables = production_test_report.get("deliverables")
    if not isinstance(deliverables, dict):
        return {
            "schema_version": "live_smoke_artifact_downloads.v1",
            "status": "fail",
            "checks": [],
            "issues": ["production_test_report_deliverables_missing"],
        }
    checks = [
        artifact_download_check(
            client,
            api_base,
            name,
            deliverables.get(name),
            artifact_output_dir=artifact_output_dir,
        )
        for name in ("final_render", "export_package", "production_manifest")
    ]
    issues = [
        f"{check['name']}:{check['status']}"
        for check in checks
        if check.get("status") != "pass"
    ]
    return {
        "schema_version": "live_smoke_artifact_downloads.v1",
        "status": "pass" if not issues else "fail",
        "artifact_output_dir": str(artifact_output_dir) if artifact_output_dir else None,
        "checks": checks,
        "issues": issues,
    }


def artifact_download_check(
    client: httpx.Client,
    api_base: str,
    name: str,
    evidence: Any,
    *,
    artifact_output_dir: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {"name": name, "status": "fail", "reason": "missing_asset_evidence"}
    download_url = evidence.get("download_url")
    if not download_url:
        return {
            "name": name,
            "status": "fail",
            "asset_id": evidence.get("asset_id"),
            "reason": evidence.get("download_missing_reason") or "download_url_missing",
        }
    url = f"{api_base}{download_url}" if str(download_url).startswith("/") else str(download_url)
    try:
        if artifact_output_dir is not None:
            return save_artifact_download(
                client,
                url,
                name,
                evidence,
                artifact_output_dir,
            )
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            byte_count = 0
            for chunk in response.iter_bytes():
                byte_count += len(chunk)
                if byte_count > 0:
                    break
            headers = response.headers
            status = (
                "pass"
                if response.status_code in {200, 206} and byte_count > 0
                else "fail"
            )
            return {
                "name": name,
                "status": status,
                "asset_id": evidence.get("asset_id"),
                "response_status_code": response.status_code,
                "bytes_read": byte_count,
                "content_type": headers.get("content-type"),
                "content_length": headers.get("content-length"),
                "content_range": headers.get("content-range"),
                "content_disposition": headers.get("content-disposition"),
            }
    except httpx.HTTPError as exc:
        return {
            "name": name,
            "status": "fail",
            "asset_id": evidence.get("asset_id"),
            "error": f"{type(exc).__name__}: {exc}",
        }


def save_artifact_download(
    client: httpx.Client,
    url: str,
    name: str,
    evidence: dict[str, Any],
    artifact_output_dir: Path,
) -> dict[str, Any]:
    artifact_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = artifact_output_dir / ARTIFACT_OUTPUT_FILENAMES.get(
        name,
        f"{name}.bin",
    )
    with client.stream("GET", url) as response:
        response.raise_for_status()
        digest = hashlib.sha256()
        byte_count = 0
        with output_path.open("wb") as handle:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
        checksum = f"sha256:{digest.hexdigest()}"
        expected_checksum = evidence.get("checksum")
        status = "pass" if byte_count > 0 else "fail"
        issues: list[str] = []
        if byte_count <= 0:
            issues.append("empty_download")
        if isinstance(expected_checksum, str) and expected_checksum:
            normalized_expected = (
                expected_checksum
                if expected_checksum.startswith("sha256:")
                else f"sha256:{expected_checksum}"
            )
            if normalized_expected != checksum:
                status = "fail"
                issues.append("checksum_mismatch")
        return {
            "name": name,
            "status": status,
            "asset_id": evidence.get("asset_id"),
            "response_status_code": response.status_code,
            "bytes_read": byte_count,
            "content_type": response.headers.get("content-type"),
            "content_length": response.headers.get("content-length"),
            "content_disposition": response.headers.get("content-disposition"),
            "saved_path": str(output_path),
            "sha256": checksum,
            "issues": issues,
        }


def production_acceptance_summary(
    *,
    episode: dict[str, Any],
    completion: dict[str, Any],
    production_test_report: dict[str, Any],
    package_inspection: dict[str, Any],
    artifact_download_checks: dict[str, Any],
    discussion_speaker_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deliverables = (
        production_test_report.get("deliverables")
        if isinstance(production_test_report.get("deliverables"), dict)
        else {}
    )
    downloads = artifact_download_checks.get("checks")
    download_checks = downloads if isinstance(downloads, list) else []
    speaker_coverage = discussion_speaker_coverage or {}
    return {
        "schema_version": "production_acceptance_summary.v1",
        "status": (
            "pass"
            if episode.get("status") == "COMPLETED"
            and completion.get("status") == "pass"
            and production_test_report.get("status") == "pass"
            and package_inspection.get("status") == "pass"
            and artifact_download_checks.get("status") == "pass"
            and (
                not speaker_coverage
                or speaker_coverage.get("status") == "pass"
            )
            else "fail"
        ),
        "episode_id": episode.get("id"),
        "episode_status": episode.get("status"),
        "production_target": production_test_report.get("production_target"),
        "production_target_satisfied": production_test_report.get(
            "production_target_satisfied"
        ),
        "completion_status": completion.get("status"),
        "production_test_status": production_test_report.get("status"),
        "package_inspection_status": package_inspection.get("status"),
        "artifact_download_status": artifact_download_checks.get("status"),
        "discussion_speaker_coverage_status": speaker_coverage.get("status"),
        "blockers": [
            *production_test_report.get("blockers", []),
            *(
                ["selected_cast_speaker_coverage_incomplete"]
                if speaker_coverage and speaker_coverage.get("status") != "pass"
                else []
            ),
        ],
        "failed_checks": completion.get("failed_checks", []),
        "discussion_speaker_coverage": compact_discussion_speaker_coverage(
            speaker_coverage
        ),
        "deliverables": {
            name: compact_deliverable_summary(deliverables.get(name))
            for name in ("final_render", "export_package", "production_manifest")
        },
        "package": {
            "package_asset_id": package_inspection.get("package_asset_id"),
            "file_count": package_inspection.get("file_count"),
            "manifest_schema_version": package_inspection.get("manifest_schema_version"),
            "chapter_count": package_inspection.get("chapter_count"),
            "subtitle_count": package_inspection.get("subtitle_count"),
            "evidence_source_count": package_inspection.get("evidence_source_count"),
            "manifest_matches_asset_metadata": package_inspection.get(
                "manifest_matches_asset_metadata"
            ),
            "issues": package_inspection.get("issues", []),
        },
        "publish_evidence": compact_publish_evidence_summary(production_test_report),
        "workflow_run_until_blocked": compact_workflow_run_until_blocked_summary(
            production_test_report
        ),
        "real_life_test_readiness": compact_real_life_test_readiness(
            production_test_report
        ),
        "downloads": {
            str(check.get("name")): compact_download_summary(check)
            for check in download_checks
            if isinstance(check, dict)
        },
    }


def production_test_summary(production_test_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": production_test_report.get("status"),
        "production_target": production_test_report.get("production_target"),
        "production_target_satisfied": production_test_report.get(
            "production_target_satisfied"
        ),
        "audio_first_test_ready": production_test_report.get("audio_first_test_ready"),
        "native_visual_test_ready": production_test_report.get("native_visual_test_ready"),
        "publish_status": (
            production_test_report.get("publish", {}).get("status")
            if isinstance(production_test_report.get("publish"), dict)
            else None
        ),
        "blockers": production_test_report.get("blockers", []),
        "operator_next_action": production_test_report.get("operator_next_action"),
        "real_life_test_readiness": compact_real_life_test_readiness(
            production_test_report
        ),
    }


def compact_real_life_test_readiness(production_test_report: dict[str, Any]) -> dict[str, Any]:
    readiness = production_test_report.get("real_life_test_readiness")
    if not isinstance(readiness, dict):
        return {
            "schema_version": "production_real_life_test_readiness_summary.v1",
            "status": "missing",
            "ready": False,
        }
    return {
        "schema_version": "production_real_life_test_readiness_summary.v1",
        "source_schema_version": readiness.get("schema_version"),
        "status": "pass" if readiness.get("ready") is True else "fail",
        "ready": readiness.get("ready") is True,
        "recommended_mode": readiness.get("recommended_mode"),
        "audio_first_ready": readiness.get("audio_first_ready") is True,
        "native_visual_ready": readiness.get("native_visual_ready") is True,
        "live_provider_preflight_ready": (
            readiness.get("live_provider_preflight_ready") is True
        ),
        "managed_media_smoke_ready": readiness.get("managed_media_smoke_ready") is True,
        "audio_first_blockers": [
            str(blocker)
            for blocker in readiness.get("audio_first_blockers", [])
            if blocker
        ][:6],
        "native_visual_blockers": [
            str(blocker)
            for blocker in readiness.get("native_visual_blockers", [])
            if blocker
        ][:8],
        "next_action": readiness.get("next_action"),
    }


def refresh_production_test_report_after_handoff(
    result: dict[str, Any],
    current_report: dict[str, Any],
) -> dict[str, Any]:
    episode_id = result.get("episode_id")
    api_base = result.get("api_base")
    requirements_update = (
        result.get("requirements_update")
        if isinstance(result.get("requirements_update"), dict)
        else {}
    )
    if not episode_id or not api_base:
        requirements_update["report_refresh"] = {
            "status": "skipped",
            "reason": "missing_episode_or_api_base",
        }
        return current_report
    try:
        with httpx.Client(timeout=300) as client:
            refreshed = get_json(
                client,
                f"{api_base}/api/v1/episodes/{episode_id}/production-test-report",
            )
    except (httpx.HTTPError, ValueError) as exc:
        requirements_update["report_refresh"] = {
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return current_report
    requirements_update["report_refresh"] = {
        "status": "pass",
        "provider_repair_handoff_status": (
            refreshed.get("provider_repair_handoff", {}).get("status")
            if isinstance(refreshed.get("provider_repair_handoff"), dict)
            else None
        ),
    }
    return refreshed


def append_provider_requirements_and_refresh_report(
    result: dict[str, Any],
    production_test_report: dict[str, Any],
    *,
    live_provider_readiness: dict[str, Any],
    requirements_output: Path,
    disabled: bool = False,
) -> dict[str, Any]:
    if disabled:
        return production_test_report
    requirements_update = append_episode_provider_requirements(
        requirements_output,
        result,
        production_test_report=production_test_report,
        live_provider_readiness=live_provider_readiness,
    )
    if requirements_update.get("appended") is not True:
        return production_test_report
    result["requirements_update"] = requirements_update
    return refresh_production_test_report_after_handoff(result, production_test_report)


def append_episode_provider_requirements(
    path: Path,
    result: dict[str, Any],
    *,
    production_test_report: dict[str, Any],
    live_provider_readiness: dict[str, Any],
) -> dict[str, Any]:
    issues = episode_provider_requirement_issues(
        production_test_report=production_test_report,
        live_provider_readiness=live_provider_readiness,
    )
    if not issues["voicebox"] and not issues["managed_media"] and not issues["comfyui"]:
        return {"path": str(path), "appended": False, "reason": "no_b1_provider_issue"}

    added_at = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        f"### DialectiCore Production Provider Handoff Added {added_at}",
        "",
        (
            "DialectiCore could not complete a talkshow production run because "
            "one or more B1 media/provider capabilities are failing or unavailable."
        ),
        (
            "This note is written for Codex on the remote B1 server to diagnose "
            "and fix the appliance side."
        ),
        "",
        "- script: `scripts/live_episode_smoke.py`",
        f"- API base: `{result.get('api_base')}`",
        f"- episode_id: `{result.get('episode_id')}`",
        f"- production target: `{production_test_report.get('production_target')}`",
        f"- production report status: `{production_test_report.get('status')}`",
        f"- operator_next_action: `{production_test_report.get('operator_next_action')}`",
        f"- live provider readiness status: `{live_provider_readiness.get('status')}`",
        "",
    ]
    if issues["voicebox"]:
        lines.extend(
            [
                "Voicebox issues:",
                "",
                *[f"- {line}" for line in issues["voicebox"]],
                "",
            ]
        )
    if issues["managed_media"]:
        lines.extend(
            [
                "B1 managed-media issues:",
                "",
                *[f"- {line}" for line in issues["managed_media"]],
                "",
            ]
        )
    if issues["comfyui"]:
        lines.extend(
            [
                "Native ComfyUI gateway issues:",
                "",
                *[f"- {line}" for line in issues["comfyui"]],
                "",
            ]
        )
    lines.extend(
        [
            "Acceptance for the B1-side fix:",
            "",
            (
                "- Voicebox generation requests for configured B1 profiles return "
                "HTTP 200 audio/wav with a non-empty RIFF/WAVE payload."
            ),
            (
                "- `GET /api/v1/system/live-provider-readiness` reports pass for "
                "the Voicebox and ComfyUI provider categories."
            ),
            (
                "- B1 managed-media catalog exposes enabled `image-default`, "
                "`image-edit`, `image-upscale`, `video-text`, and `video-image` presets."
            ),
            (
                "- B1 managed-media jobs submitted through `POST /v1/media/jobs` "
                "eventually reach `state=completed` and expose at least one "
                "downloadable artifact."
            ),
            (
                "- DialectiCore `scripts/live_episode_smoke.py` can complete the "
                "same production target without appending a new provider handoff."
            ),
            "",
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return {
        "path": str(path),
        "appended": True,
        "voicebox_issue_count": len(issues["voicebox"]),
        "managed_media_issue_count": len(issues["managed_media"]),
        "comfyui_issue_count": len(issues["comfyui"]),
    }


def episode_provider_requirement_issues(
    *,
    production_test_report: dict[str, Any],
    live_provider_readiness: dict[str, Any],
) -> dict[str, list[str]]:
    media = (
        production_test_report.get("media_readiness")
        if isinstance(production_test_report.get("media_readiness"), dict)
        else {}
    )
    return {
        "voicebox": voicebox_requirement_issue_lines(media),
        "managed_media": managed_media_requirement_issue_lines(media),
        "comfyui": comfyui_requirement_issue_lines(live_provider_readiness),
    }


def voicebox_requirement_issue_lines(media_readiness: dict[str, Any]) -> list[str]:
    audio = (
        media_readiness.get("audio_generation")
        if isinstance(media_readiness.get("audio_generation"), dict)
        else {}
    )
    action = str(media_readiness.get("audio_operator_action") or "")
    status = str(audio.get("status") or "")
    provider_ready = audio.get("provider_ready")
    if (
        action != "fix_voicebox_generation_then_retry_audio_assets"
        and status != "fail"
        and provider_ready is not False
    ):
        return []
    lines = [
        f"audio_operator_action=`{action}`",
        (
            f"audio status=`{status}`; provider_ready=`{provider_ready}`; "
            f"failed_count=`{audio.get('failed_count')}`; "
            f"voicebox_asset_count=`{audio.get('voicebox_asset_count')}`"
        ),
    ]
    for sample in audio.get("provider_issue_samples", [])[:5]:
        if not isinstance(sample, dict):
            continue
        lines.append(
            "provider sample: "
            f"endpoint_id=`{sample.get('endpoint_id')}`; "
            f"health_status=`{sample.get('health_status')}`; "
            f"adapter_type=`{sample.get('adapter_type')}`; "
            f"canary_status=`{sample.get('canary_status')}`; "
            f"canary_status_code=`{sample.get('canary_status_code')}`; "
            f"canary_riff_wave=`{sample.get('canary_riff_wave')}`"
        )
    for sample in audio.get("failure_samples", [])[:5]:
        if not isinstance(sample, dict):
            continue
        lines.append(
            "audio asset failure: "
            f"asset_id=`{sample.get('asset_id')}`; "
            f"endpoint_id=`{sample.get('voicebox_endpoint_id')}`; "
            f"voice_profile_id=`{sample.get('voice_profile_id')}`; "
            f"remote_profile_id=`{sample.get('remote_profile_id')}`; "
            f"failure_type=`{sample.get('failure_type')}`; "
            f"failure=`{sample.get('failure')}`"
        )
    return lines


def managed_media_requirement_issue_lines(media_readiness: dict[str, Any]) -> list[str]:
    execution = (
        media_readiness.get("managed_media_execution")
        if isinstance(media_readiness.get("managed_media_execution"), dict)
        else {}
    )
    smoke = (
        media_readiness.get("managed_media_smoke")
        if isinstance(media_readiness.get("managed_media_smoke"), dict)
        else {}
    )
    missing_preset_endpoints = (
        media_readiness.get("managed_media_missing_preset_endpoints")
        if isinstance(media_readiness.get("managed_media_missing_preset_endpoints"), list)
        else []
    )
    action = str(media_readiness.get("managed_media_operator_action") or "")
    execution_status = str(execution.get("status") or "")
    smoke_status = str(smoke.get("status") or "")
    has_issue = (
        action
        in {
            "fix_b1_managed_media_runner_then_rerun_smoke",
            "fix_b1_managed_media_runner_then_retry_visual_assets",
            "retry_managed_media_visual_assets_after_provider_fix",
        }
        or execution_status in {"fail", "fallback"}
        or smoke_status in {"runner_failed", "fail", "timeout"}
        or bool(missing_preset_endpoints)
    )
    if not has_issue:
        return []
    lines = [
        f"managed_media_operator_action=`{action}`",
        (
            f"execution status=`{execution_status}`; "
            f"required=`{execution.get('required')}`; "
            f"failed_count=`{execution.get('failed_count')}`; "
            f"fallback_visual_count=`{execution.get('fallback_visual_count')}`"
        ),
        (
            f"smoke status=`{smoke_status}`; model=`{smoke.get('model')}`; "
            f"operation=`{smoke.get('operation')}`; "
            f"terminal_state=`{smoke.get('terminal_state')}`; "
            f"failure_category=`{smoke.get('failure_category')}`"
        ),
    ]
    for endpoint in missing_preset_endpoints[:5]:
        if not isinstance(endpoint, dict):
            continue
        endpoint_info = (
            endpoint.get("endpoint")
            if isinstance(endpoint.get("endpoint"), dict)
            else {}
        )
        lines.append(
            "missing preset endpoint: "
            f"endpoint_id=`{endpoint_info.get('id')}`; "
            f"endpoint_name=`{endpoint_info.get('name')}`; "
            f"required_presets=`{endpoint.get('required_presets')}`; "
            f"missing_presets=`{endpoint.get('missing_presets')}`; "
            f"available_presets=`{endpoint.get('available_presets')}`"
        )
    for sample in execution.get("failure_samples", [])[:5]:
        if not isinstance(sample, dict):
            continue
        lines.append(
            "managed media failure: "
            f"asset_id=`{sample.get('asset_id')}`; "
            f"model=`{sample.get('model')}`; "
            f"operation=`{sample.get('operation')}`; "
            f"provider_state=`{sample.get('provider_state')}`; "
            f"failure_category=`{sample.get('failure_category')}`; "
            f"failure_message=`{sample.get('failure_message')}`"
        )
    return lines


def comfyui_requirement_issue_lines(live_provider_readiness: dict[str, Any]) -> list[str]:
    checks = (
        live_provider_readiness.get("checks")
        if isinstance(live_provider_readiness.get("checks"), list)
        else []
    )
    comfyui = next(
        (
            check
            for check in checks
            if isinstance(check, dict)
            and check.get("category") == "comfyui"
            and check.get("status") != "pass"
        ),
        None,
    )
    if not isinstance(comfyui, dict):
        return []
    lines = [
        (
            f"comfyui status=`{comfyui.get('status')}`; "
            f"blockers=`{comfyui.get('blockers')}`; "
            "failed_readiness_checks="
            f"`{comfyui.get('failed_readiness_checks')}`"
        ),
    ]
    for key in ("unhealthy_endpoints", "unknown_health_endpoints"):
        for endpoint in comfyui.get(key, [])[:5]:
            if not isinstance(endpoint, dict):
                continue
            lines.append(
                f"{key[:-1]}: "
                f"endpoint_id=`{endpoint.get('id')}`; "
                f"name=`{endpoint.get('name')}`; "
                f"adapter_type=`{endpoint.get('adapter_type')}`; "
                f"health_status=`{endpoint.get('health_status')}`"
            )
    return lines


def discussion_speaker_coverage_summary(
    episode: dict[str, Any],
    *,
    cast_readiness: Any,
) -> dict[str, Any]:
    selected_ids = selected_cast_participant_ids(cast_readiness)
    discussion = episode.get("discussion_session")
    turns = (
        discussion.get("turns")
        if isinstance(discussion, dict) and isinstance(discussion.get("turns"), list)
        else []
    )
    playable_turns = [
        turn
        for turn in turns
        if isinstance(turn, dict)
        and turn.get("status") != "excluded"
        and str(turn.get("speaker_participant_id") or "").strip()
    ]
    spoken_ids = sorted(
        {
            str(turn.get("speaker_participant_id") or "")
            for turn in playable_turns
            if str(turn.get("speaker_participant_id") or "") in selected_ids
        }
    )
    missing_ids = [
        participant_id
        for participant_id in selected_ids
        if participant_id not in spoken_ids
    ]
    turn_count_by_participant = {
        participant_id: sum(
            1
            for turn in playable_turns
            if str(turn.get("speaker_participant_id") or "") == participant_id
        )
        for participant_id in selected_ids
    }
    issues: list[str] = []
    if not selected_ids:
        issues.append("selected_cast_missing")
    if missing_ids:
        issues.append("selected_cast_speakers_missing")
    return {
        "schema_version": "discussion_speaker_coverage_smoke_summary.v1",
        "status": "pass" if not issues else "fail",
        "selected_participant_count": len(selected_ids),
        "playable_turn_count": len(playable_turns),
        "covered_participant_count": len(spoken_ids),
        "selected_participant_ids": selected_ids,
        "spoken_participant_ids": spoken_ids,
        "missing_participant_ids": missing_ids,
        "turn_count_by_participant": turn_count_by_participant,
        "issues": issues,
    }


def selected_cast_participant_ids(cast_readiness: Any) -> list[str]:
    if not isinstance(cast_readiness, dict):
        return []
    participants = cast_readiness.get("participants")
    if not isinstance(participants, list):
        return []
    return [
        participant_id
        for item in participants
        if isinstance(item, dict)
        and (participant_id := str(item.get("participant_id") or "").strip())
    ]


def compact_discussion_speaker_coverage(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing"}
    return {
        "status": value.get("status"),
        "selected_participant_count": value.get("selected_participant_count"),
        "covered_participant_count": value.get("covered_participant_count"),
        "playable_turn_count": value.get("playable_turn_count"),
        "missing_participant_ids": value.get("missing_participant_ids", []),
        "turn_count_by_participant": value.get("turn_count_by_participant", {}),
        "issues": value.get("issues", []),
    }


def native_visual_preflight_summary(pilot: dict[str, Any]) -> dict[str, Any]:
    stages = pilot.get("stages")
    stage_list = stages if isinstance(stages, list) else []
    visual_stage = next(
        (
            stage
            for stage in stage_list
            if isinstance(stage, dict) and stage.get("category") == "visuals"
        ),
        {},
    )
    modes = pilot.get("pilot_modes")
    mode_list = modes if isinstance(modes, list) else []
    native_mode = next(
        (
            mode
            for mode in mode_list
            if isinstance(mode, dict) and mode.get("mode") == "native_visual"
        ),
        {},
    )
    details = (
        visual_stage.get("details")
        if isinstance(visual_stage.get("details"), dict)
        else {}
    )
    checks = (
        details.get("readiness_checks")
        if isinstance(details.get("readiness_checks"), dict)
        else {}
    )
    blocked = details.get("prompt_admission_blocked_endpoints")
    blocked_endpoints = blocked if isinstance(blocked, list) else []
    return {
        "schema_version": "native_visual_preflight_summary.v1",
        "status": native_mode.get("status") or visual_stage.get("status"),
        "visual_stage_status": visual_stage.get("status"),
        "native_visual_mode_status": native_mode.get("status"),
        "prompt_admission_ready": checks.get(
            "selected_native_comfyui_prompt_admission_ready"
        ),
        "prompt_admission_blocked_endpoint_count": len(blocked_endpoints),
        "prompt_admission_blocked_endpoints": [
            compact_prompt_admission_blocker(entry)
            for entry in blocked_endpoints
            if isinstance(entry, dict)
        ],
        "blockers": native_mode.get("blockers", visual_stage.get("blockers", [])),
        "warnings": native_mode.get("warnings", visual_stage.get("warnings", [])),
    }


def compact_prompt_admission_blocker(value: dict[str, Any]) -> dict[str, Any]:
    endpoint = value.get("endpoint") if isinstance(value.get("endpoint"), dict) else {}
    admission = (
        endpoint.get("prompt_admission")
        if isinstance(endpoint.get("prompt_admission"), dict)
        else {}
    )
    return {
        "endpoint_id": endpoint.get("id"),
        "endpoint_name": endpoint.get("name"),
        "health_status": endpoint.get("health_status"),
        "admission_ready": admission.get("ready"),
        "admission_status_code": admission.get("status_code"),
        "admission_code": admission.get("code"),
        "admission_message": admission.get("message"),
        "admission_detail": admission.get("detail"),
        "participant_ids": value.get("participant_ids", []),
        "workflow_ids": value.get("workflow_ids", []),
        "visual_profile_ids": value.get("visual_profile_ids", []),
    }


def compact_deliverable_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing"}
    return {
        "asset_id": value.get("asset_id"),
        "status": value.get("status"),
        "checksum": value.get("checksum"),
        "mime_type": value.get("mime_type"),
        "downloadable": value.get("downloadable"),
        "file_size_bytes": value.get("file_size_bytes"),
        "download_missing_reason": value.get("download_missing_reason"),
    }


def compact_download_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": value.get("status"),
        "asset_id": value.get("asset_id"),
        "response_status_code": value.get("response_status_code"),
        "bytes_read": value.get("bytes_read"),
        "content_type": value.get("content_type"),
        "content_range": value.get("content_range"),
    }


def compact_publish_evidence_summary(report: dict[str, Any]) -> dict[str, Any]:
    acceptance_summary = (
        report.get("acceptance_summary")
        if isinstance(report.get("acceptance_summary"), dict)
        else {}
    )
    publish_evidence = acceptance_summary.get("publish_evidence")
    if isinstance(publish_evidence, dict):
        return {
            "status": publish_evidence.get("status"),
            "publish_job_id": publish_evidence.get("publish_job_id"),
            "job_status": publish_evidence.get("job_status"),
            "dry_run": publish_evidence.get("dry_run"),
            "package_asset_matches": publish_evidence.get("package_asset_matches"),
            "payload_package_matches": publish_evidence.get("payload_package_matches"),
            "current_manifest_embeds_publish_job": publish_evidence.get(
                "current_manifest_embeds_publish_job"
            ),
            "current_manifest_publish_job_status_matches": publish_evidence.get(
                "current_manifest_publish_job_status_matches"
            ),
            "payload_manifest_is_current": publish_evidence.get(
                "payload_manifest_is_current"
            ),
            "payload_production_manifest_schema_version": publish_evidence.get(
                "payload_production_manifest_schema_version"
            ),
        }

    binding = (
        report.get("publish_evidence_binding")
        if isinstance(report.get("publish_evidence_binding"), dict)
        else {}
    )
    if not binding:
        return {"status": "missing"}
    return {
        "status": binding.get("status"),
        "publish_job_id": binding.get("publish_job_id"),
        "job_status": binding.get("job_status"),
        "dry_run": binding.get("dry_run"),
        "package_asset_matches": binding.get("package_asset_matches"),
        "payload_package_matches": binding.get("payload_package_matches"),
        "current_manifest_embeds_publish_job": binding.get(
            "current_manifest_embeds_publish_job"
        ),
        "current_manifest_publish_job_status_matches": binding.get(
            "current_manifest_publish_job_status_matches"
        ),
        "payload_manifest_is_current": binding.get("payload_manifest_is_current"),
        "payload_production_manifest_schema_version": binding.get(
            "payload_production_manifest_schema_version"
        ),
    }


def compact_workflow_run_until_blocked_summary(report: dict[str, Any]) -> dict[str, Any]:
    evidence = report.get("workflow_run_until_blocked")
    if not isinstance(evidence, dict):
        acceptance_summary = (
            report.get("acceptance_summary")
            if isinstance(report.get("acceptance_summary"), dict)
            else {}
        )
        evidence = acceptance_summary.get("workflow_run_until_blocked")
    if not isinstance(evidence, dict):
        return {
            "schema_version": "production_workflow_run_until_blocked_summary.v1",
            "status": "missing",
        }
    attempt_ids = [
        str(value)
        for value in evidence.get("orchestration_attempt_ids", [])
        if value
    ][:10]
    return {
        "schema_version": evidence.get(
            "schema_version", "production_workflow_run_until_blocked_summary.v1"
        ),
        "source_schema_version": evidence.get("source_schema_version"),
        "recorded_at": evidence.get("recorded_at"),
        "status": evidence.get("status"),
        "stop_reason": evidence.get("stop_reason"),
        "pass_count": evidence.get("pass_count"),
        "progressed_stage_count": evidence.get("progressed_stage_count"),
        "pending_approval_count": evidence.get("pending_approval_count"),
        "pending_approval_stages": [
            str(stage)
            for stage in evidence.get("pending_approval_stages", [])
            if stage
        ][:8],
        "completion_status": evidence.get("completion_status"),
        "completion_failed_checks": [
            str(check)
            for check in evidence.get("completion_failed_checks", [])
            if check
        ][:8],
        "handoff": compact_run_until_blocked_handoff(evidence.get("handoff")),
        "orchestration_attempt_count": (
            evidence.get("orchestration_attempt_count") or len(attempt_ids)
        ),
        "orchestration_attempt_ids": attempt_ids,
    }


def approve_pending_gates(
    client: httpx.Client,
    api_base: str,
    episode: dict[str, Any],
    *,
    user_id: str,
) -> list[dict[str, Any]]:
    results = []
    for approval in episode.get("approvals", []):
        if not isinstance(approval, dict) or approval.get("decision") != "pending":
            continue
        stage = str(approval.get("stage") or "")
        comment = APPROVAL_COMMENTS.get(stage)
        if not comment:
            continue
        approval_id = approval.get("id")
        if not approval_id:
            continue
        response = client.post(
            f"{api_base}/api/v1/episodes/{episode['id']}/approvals/{approval_id}/decision",
            json={"decision": "approved", "user_id": user_id, "comment": comment},
        )
        if response.status_code == 404:
            results.append(
                {
                    "stage": stage,
                    "approval_id": approval_id,
                    "decision": "skipped",
                    "reason": "approval_not_found_or_superseded",
                }
            )
            continue
        response.raise_for_status()
        results.append({"stage": stage, "approval_id": approval_id, "decision": "approved"})
    return results


def pending_approval_count(episode: dict[str, Any]) -> int:
    return sum(
        1
        for approval in episode.get("approvals", [])
        if isinstance(approval, dict) and approval.get("decision") == "pending"
    )


def run_episode_until_blocked(
    client: httpx.Client,
    api_base: str,
    episode_id: str,
    *,
    user_id: str,
    max_passes: int = 4,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/api/v1/episodes/{episode_id}/workflow/run-until-blocked",
        json={
            "start_if_needed": True,
            "max_passes": max_passes,
            "user_id": user_id,
            "comment": "Live smoke run until the next review gate or completion.",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("workflow run-until-blocked response was not an object")
    if not isinstance(payload.get("episode"), dict):
        raise ValueError("workflow run-until-blocked response omitted episode")
    return payload


def run_until_blocked_summary(result: dict[str, Any]) -> dict[str, Any]:
    summaries = result.get("summaries") if isinstance(result.get("summaries"), list) else []
    latest_summary = next(
        (summary for summary in reversed(summaries) if isinstance(summary, dict)),
        {},
    )
    latest_stages = (
        latest_summary.get("stages")
        if isinstance(latest_summary.get("stages"), dict)
        else {}
    )
    pending = (
        result.get("pending_approvals")
        if isinstance(result.get("pending_approvals"), list)
        else []
    )
    return {
        "schema_version": "live_smoke_run_until_blocked_summary.v1",
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
        "pass_count": result.get("pass_count"),
        "progressed_stage_count": result.get("progressed_stage_count"),
        "handoff": compact_run_until_blocked_handoff(result.get("handoff")),
        "pending_approval_count": len([item for item in pending if isinstance(item, dict)]),
        "pending_approval_stages": [
            item.get("stage") for item in pending if isinstance(item, dict)
        ],
        "latest_render": stage_counts(latest_stages.get("render")),
        "latest_publishing": stage_counts(latest_stages.get("publishing")),
        "latest_completion": stage_counts(latest_stages.get("completion")),
    }


def compact_run_until_blocked_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
            "status": "missing",
            "blocking_reasons": [],
        }
    stage_readiness = (
        value.get("stage_readiness") if isinstance(value.get("stage_readiness"), dict) else {}
    )
    asset_ids = value.get("asset_ids") if isinstance(value.get("asset_ids"), dict) else {}
    character_configuration = (
        value.get("character_configuration")
        if isinstance(value.get("character_configuration"), dict)
        else {}
    )
    turn_handoffs = (
        value.get("turn_handoffs") if isinstance(value.get("turn_handoffs"), dict) else {}
    )
    return {
        "schema_version": value.get(
            "schema_version", "workflow_run_until_blocked_handoff_summary.v1"
        ),
        "source_schema_version": value.get("source_schema_version"),
        "status": value.get("status"),
        "next_handoff_action": value.get("next_handoff_action"),
        "blocking_reasons": [
            str(reason) for reason in value.get("blocking_reasons", []) if reason
        ][:8],
        "playable_turn_count": value.get("playable_turn_count"),
        "character_configuration": {
            "ready": character_configuration.get("ready"),
            "missing_model_participant_ids": [
                str(item)
                for item in character_configuration.get("missing_model_participant_ids", [])
                if item
            ][:8],
            "missing_voice_participant_ids": [
                str(item)
                for item in character_configuration.get("missing_voice_participant_ids", [])
                if item
            ][:8],
            "missing_visual_participant_ids": [
                str(item)
                for item in character_configuration.get("missing_visual_participant_ids", [])
                if item
            ][:8],
        },
        "turn_handoffs": {
            "completed_audio_turn_count": turn_handoffs.get("completed_audio_turn_count"),
            "completed_primary_visual_turn_count": turn_handoffs.get(
                "completed_primary_visual_turn_count"
            ),
            "missing_audio_turn_count": len(
                [
                    item
                    for item in turn_handoffs.get("missing_audio_turn_ids", [])
                    if item
                ]
            ),
            "missing_primary_visual_turn_count": len(
                [
                    item
                    for item in turn_handoffs.get("missing_primary_visual_turn_ids", [])
                    if item
                ]
            ),
        },
        "stage_readiness": {
            key: stage_readiness.get(key)
            for key in (
                "speech",
                "character_animation",
                "studio_scene",
                "subtitles",
                "timeline",
                "publish",
                "preview_render_approved",
                "final_render_approved",
            )
            if key in stage_readiness
        },
        "asset_ids": {
            key: asset_ids.get(key)
            for key in (
                "preview_render",
                "final_render",
                "delivery_package",
                "production_manifest",
                "publish_job",
            )
            if asset_ids.get(key)
        },
    }


def advance_episode(client: httpx.Client, api_base: str, episode_id: str) -> dict[str, Any]:
    response = client.post(f"{api_base}/api/v1/episodes/{episode_id}/workflow/advance")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("workflow advance response was not an object")
    return payload


def advance_summary(advance: dict[str, Any]) -> dict[str, Any]:
    summary = advance.get("summary") if isinstance(advance.get("summary"), dict) else {}
    episode = advance.get("episode") if isinstance(advance.get("episode"), dict) else {}
    stages = summary.get("stages") if isinstance(summary.get("stages"), dict) else {}
    return {
        "episode_status": episode.get("status"),
        "progressed_stage_count": summary.get("progressed_stage_count"),
        "error_count": summary.get("error_count"),
        "render": stage_counts(stages.get("render")),
        "publishing": stage_counts(stages.get("publishing")),
        "completion": stage_counts(stages.get("completion")),
    }


def stage_counts(stage: Any) -> dict[str, Any]:
    if not isinstance(stage, dict):
        return {}
    interesting = [
        "preview_renders_created",
        "final_renders_created",
        "thumbnails_created",
        "youtube_packages_created",
        "production_manifests_created",
        "production_manifests_refreshed",
        "dry_run_publish_jobs_created",
        "episodes_completed",
        "readiness_blocked",
        "error_count",
    ]
    return {key: stage[key] for key in interesting if key in stage}


def episode_summary(episode: dict[str, Any]) -> dict[str, Any]:
    assets = episode.get("assets", [])
    publish_jobs = episode.get("publish_jobs", [])
    discussion = episode.get("discussion_session") or {}
    definition = episode.get("definition") if isinstance(episode.get("definition"), dict) else {}
    workflow = definition.get("workflow") if isinstance(definition.get("workflow"), dict) else {}
    format_settings = definition.get("format") if isinstance(definition.get("format"), dict) else {}
    return {
        "id": episode.get("id"),
        "title": episode.get("title"),
        "status": episode.get("status"),
        "production_target": workflow.get("production_target"),
        "target_duration_minutes": format_settings.get("target_duration_minutes"),
        "permitted_deviation_percent": format_settings.get("permitted_deviation_percent"),
        "turn_count": len(discussion.get("turns") or []) if isinstance(discussion, dict) else 0,
        "estimated_duration_seconds": (
            discussion.get("estimated_duration_seconds") if isinstance(discussion, dict) else None
        ),
        "asset_counts": asset_counts(assets if isinstance(assets, list) else []),
        "publish_jobs": [
            {
                "id": job.get("id"),
                "status": job.get("status"),
                "dry_run": job.get("dry_run"),
                "publisher_target_id": job.get("publisher_target_id"),
            }
            for job in publish_jobs
            if isinstance(job, dict)
        ],
    }


def asset_counts(assets: list[Any]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_type = str(asset.get("asset_type") or "unknown")
        status = str(asset.get("status") or "unknown")
        by_status = counts.setdefault(asset_type, {})
        by_status[status] = by_status.get(status, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
