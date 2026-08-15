from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/production_v2_register_thumbnail.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("production_v2_thumbnail", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_thumbnail_probe_requires_delivery_resolution_and_jpeg() -> None:
    module = _module()

    module._validate_probe(
        {"width": 1920, "height": 1080, "video_codec": "mjpeg"}
    )
    with pytest.raises(RuntimeError, match="1920x1080"):
        module._validate_probe(
            {"width": 1280, "height": 720, "video_codec": "mjpeg"}
        )
    with pytest.raises(RuntimeError, match="JPEG"):
        module._validate_probe(
            {"width": 1920, "height": 1080, "video_codec": "png"}
        )
