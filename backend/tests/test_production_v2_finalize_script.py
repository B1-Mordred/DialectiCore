from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _module() -> Any:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts/production_v2_finalize_approved_preview.py"
    )
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("production_v2_finalize", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finalizer_transcodes_the_approved_composite_as_one_program(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "preview.mp4"
    target = tmp_path / "final.partial.mp4"

    command = module._ffmpeg_command(source, target)

    assert command[:5] == ["ffmpeg", "-hide_banner", "-y", "-i", str(source)]
    assert "scale=1920:1080:flags=lanczos,fps=30" in command
    assert command.count("-map") == 2
    assert "+faststart" in command
    assert command[-1] == str(target)


def test_finalizer_accepts_only_delivery_probe_matching_the_approved_program() -> None:
    module = _module()
    source = {"duration_ms": 364_333}
    delivery = {
        "duration_ms": 364_333,
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_sample_rate": 48_000,
        "audio_channels": 2,
        "av_offset_ms": 0,
    }

    module._validate_delivery_probe(source, delivery)
    assert module._delivery_duration_limit_seconds(364_333) == 365

    with pytest.raises(RuntimeError, match="duration"):
        module._validate_delivery_probe(source, {**delivery, "duration_ms": 360_000})

