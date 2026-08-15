#!/usr/bin/env python3
"""Render the production-v2 six-speaker studio/B-roll qualification sequence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "output/production-v2/integrated-qualification"
ANIMATION_ROOT = ROOT / "output/production-v2/animation-qualification"
MASTER_ROOT = ROOT / "output/production-v2/normalized-seated-masters"
STUDIO_URI = "object://dialecticore/show-media/scene-reference-images/47d9f89bed32daac.png"
PARTICIPANTS = ("chatgpt", "claude", "deepseek", "gemini", "grok", "mistral")
SELECTED_CANDIDATE = {
    "chatgpt": "v2_normalized_master",
    "claude": "v2_normalized_master",
    "deepseek": "v2_detector_source_crop",
    "gemini": "v2_normalized_master",
    "grok": "v2_normalized_master",
    "mistral": "v2_normalized_master",
}
SEAT_CENTERS_X = (350, 545, 740, 935, 1130, 1325)
# The five normalized masters have a 1280px canvas and an alpha baseline at
# y=1119.  DeepSeek's detector-compatible input is a 640px crop with its alpha
# baseline at y=445.  Scaling every *canvas* to 330px therefore made DeepSeek's
# actual body about 20 percent shorter.  Keep per-input canvas sizes and anchor
# the resulting alpha baseline behind the desk instead of moving a small body
# upward by an arbitrary top coordinate.
CHARACTER_CANVAS_SIZE = {
    "chatgpt": 330,
    "claude": 330,
    "deepseek": 414,
    "gemini": 330,
    "grok": 330,
    "mistral": 330,
}
MATTE_GEOMETRY = {
    "chatgpt": {"canvas": 1280, "alpha_bottom": 1119},
    "claude": {"canvas": 1280, "alpha_bottom": 1119},
    "deepseek": {"canvas": 640, "alpha_bottom": 445},
    "gemini": {"canvas": 1280, "alpha_bottom": 1119},
    "grok": {"canvas": 1280, "alpha_bottom": 1119},
    "mistral": {"canvas": 1280, "alpha_bottom": 1119},
}
CAMERA_WIDTH = 800
CAMERA_HEIGHT = 450
CAMERA_TOP = 190
CAMERA_EDGE_EXTENSION = CAMERA_WIDTH // 2
SCREEN_X = 294
SCREEN_Y = 190
SCREEN_WIDTH = 1065
SCREEN_HEIGHT = 400
DESK_TOP = 584
DESK_OCCLUSION_OVERLAP = 12
SEGMENT_SECONDS = 4
DEFAULT_PRESENTATION_TRANSITION_SECONDS = 2.0


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-3000:]}"
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_studio(path: Path) -> None:
    query = urllib.parse.urlencode({"uri": STUDIO_URI})
    url = f"http://127.0.0.1:8000/api/v1/show-media/scene-reference-image/download?{query}"
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        path.write_bytes(response.read())


def _master_path(participant_id: str) -> Path:
    suffix = "detector-source-crop.png" if participant_id == "deepseek" else "master.png"
    return MASTER_ROOT / f"{participant_id}-{suffix}"


def _animation_path(participant_id: str) -> Path:
    return ANIMATION_ROOT / participant_id / f"{SELECTED_CANDIDATE[participant_id]}.mp4"


def _participant_source_input_index(participant_id: str) -> int:
    # Inputs 0 and 1 are the studio and B-roll. Each participant then adds a
    # source input followed by its alpha matte input.
    return 2 + PARTICIPANTS.index(participant_id) * 2


def _character_layout(participant_id: str) -> dict[str, int]:
    size = CHARACTER_CANVAS_SIZE[participant_id]
    geometry = MATTE_GEOMETRY[participant_id]
    scaled_alpha_bottom = round(geometry["alpha_bottom"] * size / geometry["canvas"])
    target_alpha_bottom = DESK_TOP + DESK_OCCLUSION_OVERLAP
    return {
        "canvas_size": size,
        "left": SEAT_CENTERS_X[PARTICIPANTS.index(participant_id)] - size // 2,
        "top": target_alpha_bottom - scaled_alpha_bottom,
        "target_alpha_bottom": target_alpha_bottom,
    }


def _presentation_blend(index: int, transition_seconds: float) -> str:
    """Return an eased studio/fullscreen blend for the qualification round trip."""
    duration = max(0.25, min(float(transition_seconds), SEGMENT_SECONDS - 0.5))
    duration_frames = round(duration * 24)
    if index == 2:
        start_frame = 12
        end_frame = start_frame + duration_frames
        progress = f"(0.5-0.5*cos(PI*(N-{start_frame})/{duration_frames}))"
        return (
            f"if(lt(N,{start_frame}),A,"
            f"if(lt(N,{end_frame}),A*(1-{progress})+B*{progress},B))"
        )
    if index == 3:
        start_frame = round((SEGMENT_SECONDS - 0.5 - duration) * 24)
        end_frame = start_frame + duration_frames
        progress = f"(0.5-0.5*cos(PI*(N-{start_frame})/{duration_frames}))"
        return (
            f"if(lt(N,{start_frame}),B,"
            f"if(lt(N,{end_frame}),B*(1-{progress})+A*{progress},A))"
        )
    return "A"


def _render_segment(
    *,
    index: int,
    participant_id: str,
    studio: Path,
    broll: Path,
    output: Path,
    presentation_transition_seconds: float,
) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    command.extend(["-loop", "1", "-i", str(studio)])
    command.extend(
        [
            "-stream_loop",
            "-1",
            "-ss",
            str(index * SEGMENT_SECONDS),
            "-i",
            str(broll),
        ]
    )
    for seated_id in PARTICIPANTS:
        source = (
            _animation_path(seated_id) if seated_id == participant_id else _master_path(seated_id)
        )
        if source.suffix == ".mp4":
            command.extend(["-stream_loop", "-1", "-i", str(source)])
        else:
            command.extend(["-loop", "1", "-i", str(source)])
        # The qualified talking-head output is RGB; the matching normalized
        # source provides the separately preserved silhouette matte.
        command.extend(["-loop", "1", "-i", str(_master_path(seated_id))])

    filter_parts = [
        "[0:v]scale=1672:941,format=rgba[studio]",
        (
            f"[1:v]scale={SCREEN_WIDTH}:{SCREEN_HEIGHT}:"
            "force_original_aspect_ratio=increase,"
            f"crop={SCREEN_WIDTH}:{SCREEN_HEIGHT},fps=24,format=rgba[screen]"
        ),
        f"[studio][screen]overlay={SCREEN_X}:{SCREEN_Y}:format=auto[screenbase]",
    ]
    previous = "screenbase"
    input_index = 2
    for seat_index, seated_id in enumerate(PARTICIPANTS):
        source_index = input_index
        matte_index = input_index + 1
        layout = _character_layout(seated_id)
        character_size = layout["canvas_size"]
        x = layout["left"]
        if seated_id == participant_id:
            filter_parts.extend(
                [
                    (
                        f"[{source_index}:v]scale={character_size}:{character_size}:"
                        "force_original_aspect_ratio=decrease,"
                        f"pad={character_size}:{character_size}:(ow-iw)/2:(oh-ih)/2,"
                        "format=rgb24[active_rgb]"
                    ),
                    (
                        f"[{matte_index}:v]alphaextract,"
                        f"scale={character_size}:{character_size}:"
                        "force_original_aspect_ratio=decrease,"
                        f"pad={character_size}:{character_size}:(ow-iw)/2:(oh-ih)/2:color=black"
                        "[active_alpha]"
                    ),
                    "[active_rgb][active_alpha]alphamerge[character]",
                ]
            )
        else:
            filter_parts.append(
                f"[{source_index}:v]scale={character_size}:{character_size}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={character_size}:{character_size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                "eq=brightness=-0.12:saturation=0.55,format=rgba[character]"
            )
        next_label = f"people{seat_index}"
        filter_parts.append(
            f"[{previous}][character]overlay={x}:{layout['top']}:format=auto[{next_label}]"
        )
        previous = next_label
        input_index += 2

    # Reapply the original desk as a foreground plate. This produces natural
    # occlusion while allowing a small lower part of the rear screen to remain
    # hidden behind the seated cast.
    filter_parts.extend(
        [
            f"[0:v]crop=1672:{941 - DESK_TOP}:0:{DESK_TOP},format=rgba[desk]",
            f"[{previous}][desk]overlay=0:{DESK_TOP}:format=auto[studio_people]",
        ]
    )
    camera_x = SEAT_CENTERS_X[index]
    filter_parts.extend(
        [
            (
                f"[studio_people]pad={1672 + CAMERA_EDGE_EXTENSION * 2}:941:"
                f"{CAMERA_EDGE_EXTENSION}:0:color=0x070b23[extended_studio]"
            ),
            (
                f"[extended_studio]crop={CAMERA_WIDTH}:{CAMERA_HEIGHT}:"
                f"{camera_x}:{CAMERA_TOP},"
                "scale=1280:720:flags=lanczos,setsar=1,setpts=N/(24*TB)[studio_camera]"
            ),
            (
                "[1:v]scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,setsar=1,setpts=N/(24*TB)[fullscreen]"
            ),
        ]
    )
    blend = _presentation_blend(index, presentation_transition_seconds)
    filter_parts.append(
        f"[studio_camera][fullscreen]blend=all_expr='{blend}',fps=24,format=yuv420p[vout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[vout]",
            "-map",
            f"{_participant_source_input_index(participant_id)}:a:0?",
            "-t",
            str(SEGMENT_SECONDS),
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-crf",
            "16",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run(command)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--broll",
        type=Path,
        default=(
            ROOT
            / "storage/object-store/dialecticore/episodes"
            / "cc1ad449-9cad-4a40-a150-652db0b7dc7a"
            / "opening-media/b196f11bebc88fe8.mp4"
        ),
    )
    parser.add_argument(
        "--presentation-transition-seconds",
        type=float,
        default=DEFAULT_PRESENTATION_TRANSITION_SECONDS,
        help="Eased studio/fullscreen B-roll transition duration (0.25-3.5 seconds).",
    )
    args = parser.parse_args()
    if not 0.25 <= args.presentation_transition_seconds <= SEGMENT_SECONDS - 0.5:
        parser.error("--presentation-transition-seconds must be between 0.25 and 3.5")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    studio = OUTPUT_ROOT / "studio-reference.png"
    _download_studio(studio)
    segments: list[Path] = []
    for index, participant_id in enumerate(PARTICIPANTS):
        segment = OUTPUT_ROOT / f"{index + 1:02d}-{participant_id}.mp4"
        _render_segment(
            index=index,
            participant_id=participant_id,
            studio=studio,
            broll=args.broll,
            output=segment,
            presentation_transition_seconds=args.presentation_transition_seconds,
        )
        segments.append(segment)
    concat = OUTPUT_ROOT / "concat.txt"
    concat.write_text("".join(f"file '{path.name}'\n" for path in segments))
    final = OUTPUT_ROOT / "production-v2-integrated-qualification.mp4"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(final),
        ]
    )
    manifest = {
        "schema_version": "dialecticore.production_v2.integrated_qualification.v2",
        "created_at": datetime.now(UTC).isoformat(),
        "studio": {
            "uri": STUDIO_URI,
            "path": str(studio.relative_to(ROOT)),
            "sha256": _sha256(studio),
        },
        "broll": {
            "path": str(args.broll.relative_to(ROOT)),
            "sha256": _sha256(args.broll),
            "source_clock_continuous": True,
            "audio_mode": "muted",
            "provenance_required": False,
        },
        "participants": [
            {
                "participant_id": participant_id,
                "selected_candidate": SELECTED_CANDIDATE[participant_id],
                "animation_path": str(_animation_path(participant_id).relative_to(ROOT)),
                "animation_sha256": _sha256(_animation_path(participant_id)),
                "master_path": str(_master_path(participant_id).relative_to(ROOT)),
                "master_sha256": _sha256(_master_path(participant_id)),
                "seat_center_x": SEAT_CENTERS_X[index],
                "camera_crop_x_in_extended_studio": SEAT_CENTERS_X[index],
                "composition_layout": _character_layout(participant_id),
            }
            for index, participant_id in enumerate(PARTICIPANTS)
        ],
        "presentation": {
            "rear_screen": {
                "x": SCREEN_X,
                "y": SCREEN_Y,
                "width": SCREEN_WIDTH,
                "height": SCREEN_HEIGHT,
            },
            "fullscreen_round_trip": {
                "transition_duration_ms": round(
                    args.presentation_transition_seconds * 1000
                ),
                "easing": "ease_in_out_cosine",
                "start_ms": 8_500,
                "fullscreen_ms": [
                    round((8.5 + args.presentation_transition_seconds) * 1000),
                    round((15.5 - args.presentation_transition_seconds) * 1000),
                ],
                "end_ms": 15_500,
            },
            "desk_foreground_occlusion_y": DESK_TOP,
            "desk_character_overlap_px": DESK_OCCLUSION_OVERLAP,
            "camera_crop": {
                "width": CAMERA_WIDTH,
                "height": CAMERA_HEIGHT,
                "top": CAMERA_TOP,
                "edge_extension": CAMERA_EDGE_EXTENSION,
                "output_width": 1280,
                "output_height": 720,
            },
        },
        "output": {
            "path": str(final.relative_to(ROOT)),
            "bytes": final.stat().st_size,
            "sha256": _sha256(final),
        },
    }
    manifest_path = OUTPUT_ROOT / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest["output"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
