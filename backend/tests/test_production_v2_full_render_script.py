from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/production_v2_full_render.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("production_v2_full_render", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job(index: int, participant_id: str, duration_ms: int = 10_000) -> dict[str, Any]:
    return {
        "index": index,
        "participant_id": participant_id,
        "duration_ms": duration_ms,
        "turn_id": f"turn-{index}",
        "audio_asset_id": f"audio-{index}",
        "text": f"Turn {index}",
        "job_id": f"job-{index}",
        "artifact_path": f"output/turn-{index}.mp4",
    }


def test_presentation_plan_keeps_opening_and_close_wide_in_the_studio() -> None:
    module = _module()

    assert module._presentation_mode(1) == "rear_screen"
    assert module._camera_mode(1) == "establishing_wide"
    assert module._camera_mode(21) == "establishing_wide"
    assert module._camera_mode(2) == "speaker_centered"
    assert module._camera_action(1) == "fly_in"
    assert module._camera_action(21) == "slow_pull"
    assert module._camera_action(2) == "cut"
    assert "zoompan" in module._wide_camera_filter("fly_in", 5_000)
    assert module._presentation_mode(9) == "enter"
    assert module._presentation_mode(10) == "exit"
    assert module._presentation_mode(14) == "roundtrip"
    assert module._presentation_mode(2) == "rear_screen"
    assert "cos" in module._presentation_blend("roundtrip", 21_000)


def test_full_production_seats_keep_outer_torsos_over_the_desk() -> None:
    module = _module()

    chatgpt = module._full_character_layout("chatgpt", wide=False)
    mistral = module._full_character_layout("mistral", wide=False)
    claude = module._full_character_layout("claude", wide=False)
    wide_chatgpt = module._full_character_layout("chatgpt", wide=True)

    # Measured scaled alpha half-widths are 120px for ChatGPT and 107px for
    # Mistral. The physical desk surface spans approximately x=313..1337.
    assert module.FULL_SEAT_CENTERS_X[0] - 120 >= 313
    assert module.FULL_SEAT_CENTERS_X[-1] + 107 <= 1337
    assert chatgpt["left"] < mistral["left"]
    assert wide_chatgpt["canvas_size"] < chatgpt["canvas_size"]
    assert wide_chatgpt["target_alpha_bottom"] == chatgpt["target_alpha_bottom"]
    assert (
        chatgpt["target_alpha_bottom"] - claude["target_alpha_bottom"]
        == module.EDGE_DESK_CONTACT_EXTRA
    )
    assert (
        mistral["target_alpha_bottom"] - claude["target_alpha_bottom"]
        == module.EDGE_DESK_CONTACT_EXTRA
    )


def test_timeline_keeps_broll_parallel_to_unbroken_dialogue() -> None:
    module = _module()
    jobs = [_job(1, "chatgpt", 21_000), _job(2, "grok", 10_260)]
    broll = [
        {
            "index": 1,
            "path": "broll.mp4",
            "sha256": "a" * 64,
            "timeline_start_ms": 0,
            "timeline_end_ms": 62_000,
            "source_in_ms": 24_000,
            "crossfade_ms": 1_500,
        }
    ]

    timeline = module._timeline(jobs, primer_duration_ms=63_927, broll_clips=broll)

    assert timeline["schema_version"] == "episode_timeline.v3"
    assert timeline["duration_ms"] == 95_187
    assert len(timeline["segments"]) == 2
    dialogue = timeline["tracks"]["dialogue"]
    assert dialogue[0]["start_ms"] == 63_927
    assert dialogue[0]["end_ms"] == dialogue[1]["start_ms"]
    assert timeline["tracks"]["broll_content"][0]["start_ms"] == 63_927
    assert timeline["tracks"]["broll_content"][0]["end_ms"] == timeline["duration_ms"]
    assert timeline["tracks"]["broll_content"][0]["source_out_ms"] == 55_260
    assert timeline["tracks"]["captions"][0]["offset_ms"] == 63_927


def test_shift_vtt_offsets_every_timestamp(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "source.vtt"
    output = tmp_path / "shifted.vtt"
    source.write_text("WEBVTT\n\n00:00:00.020 --> 00:00:02.803\nHello\n")

    module._shift_vtt(source, output, 63_927)

    assert "00:01:03.947 --> 00:01:06.730" in output.read_text()
