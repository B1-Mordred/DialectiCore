from scripts.b1_managed_media_smoke import (
    append_media_requirements,
    artifact_summaries,
    extract_job_id,
    infer_modality,
    infer_operation,
    job_summary,
    maybe_append_media_requirements,
    submit_payload_for_route,
    submit_route_for_result,
    submit_summary,
    terminal_poll,
)


def test_b1_managed_media_smoke_infers_b1_model_operation() -> None:
    assert infer_modality("image-default") == "image"
    assert infer_operation("image-default") == "image-generation"
    assert infer_operation("image-edit") == "image-edit"
    assert infer_operation("image-upscale") == "upscaling"
    assert infer_modality("video-image") == "video"
    assert infer_operation("video-image") == "image-to-video"
    assert infer_operation("video-text") == "video-generation"


def test_b1_managed_media_smoke_extracts_standard_and_compat_job_ids() -> None:
    assert extract_job_id({"id": "job-a"}) == "job-a"
    assert extract_job_id({"job_id": "job-b"}) == "job-b"
    assert extract_job_id({"b1_job_id": "job-c"}) == "job-c"
    assert extract_job_id({"data": []}) is None


def test_b1_managed_media_smoke_uses_openai_image_route_for_default_image_smoke() -> None:
    result = {
        "model": "image-default",
        "modality": "image",
        "operation": "image-generation",
    }
    payload = {
        "model": "image-default",
        "input": {
            "prompt": "small studio card",
            "negative_prompt": "text",
            "width": 128,
            "height": 128,
            "steps": 1,
            "cfg": 1.0,
            "seed": 7,
        },
    }

    route = submit_route_for_result(result)

    assert route == "/v1/images/generations"
    assert submit_payload_for_route(payload, route) == {
        "model": "image-default",
        "prompt": "small studio card",
        "n": 1,
        "size": "128x128",
        "negative_prompt": "text",
        "steps": 1,
        "cfg": 1.0,
        "seed": 7,
    }
    assert submit_route_for_result(
        {"model": "video-image", "modality": "video", "operation": "image-to-video"}
    ) == "/v1/media/jobs"


def test_b1_managed_media_smoke_summarizes_submit_and_job_payloads() -> None:
    submit = submit_summary(
        {
            "b1_job_id": "job-1",
            "b1_status": "queued",
            "operation": "image-generation",
            "modality": "image",
            "b1_job_url": "/v1/media/jobs/job-1",
            "b1_events_url": "/v1/media/jobs/job-1/events",
        }
    )
    assert submit["job_id"] == "job-1"
    assert submit["state"] == "queued"
    assert submit["links"]["b1_job_url"] == "/v1/media/jobs/job-1"

    job = job_summary(
        {
            "id": "job-1",
            "state": "failed",
            "stage": "failed",
            "progress": 100,
            "failure_category": "gpu_runner_error",
            "failure_message": "ValueError",
            "artifacts": [{"id": "a", "url": "/artifacts/a.png", "sha256": "sha"}],
        }
    )
    assert job["job_id"] == "job-1"
    assert job["state"] == "failed"
    assert job["artifact_count"] == 1
    assert job["artifacts"] == [
        {
            "id": "a",
            "kind": None,
            "mime_type": None,
            "bytes": None,
            "sha256": "sha",
            "url": "/artifacts/a.png",
            "path": None,
            "ingest_status": None,
        }
    ]


def test_b1_managed_media_smoke_terminal_and_artifact_helpers() -> None:
    assert terminal_poll([]) == {"state": "missing"}
    assert terminal_poll([{"state": "queued"}, {"state": "completed"}]) == {
        "state": "completed"
    }
    assert artifact_summaries("not-list") == []


def test_b1_managed_media_smoke_appends_codex_readable_requirements(
    tmp_path,
) -> None:
    path = tmp_path / "media-requirements.md"
    result = {
        "api_base": "https://api.ai.b1.germering",
        "model": "video-image",
        "modality": "video",
        "operation": "image-to-video",
        "poll_attempts": 2,
        "poll_interval_seconds": 1.0,
        "status": "runner_failed",
        "job_id": "job-a",
        "submit": {"job_id": "job-a", "state": "queued"},
        "terminal": {
            "job_id": "job-a",
            "state": "failed",
            "stage": "runner",
            "progress": 100,
            "failure_category": "gpu_runner_error",
            "failure_message": "ComfyUI node failed",
            "native_prompt_id": "prompt-a",
            "artifact_count": 0,
        },
    }

    update = append_media_requirements(path, result)

    text = path.read_text(encoding="utf-8")
    assert update == {"path": str(path), "appended": True}
    assert "B1 Managed Media Smoke Recheck Added" in text
    assert "scripts/b1_managed_media_smoke.py" in text
    assert "model alias: `video-image`" in text
    assert "failure category: `gpu_runner_error`" in text
    assert "failure message: `ComfyUI node failed`" in text
    assert "`video-image`: Wan 2.1 VACE 1.3B" in text
    assert "exits 0 without `--allow-runner-failure`" in text


def test_b1_managed_media_smoke_requirements_update_skips_pass(tmp_path) -> None:
    path = tmp_path / "media-requirements.md"
    result = {"status": "pass"}

    maybe_append_media_requirements(
        type("Args", (), {"requirements_output": str(path)})(),
        result,
    )

    assert not path.exists()
    assert "requirements_update" not in result
