#!/usr/bin/env python3
"""Render the complete production-v2 preview from managed B1 speaking clips."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from production_v2_integrated_qualification import (
    CAMERA_EDGE_EXTENSION,
    CAMERA_HEIGHT,
    CAMERA_TOP,
    CAMERA_WIDTH,
    DESK_TOP,
    PARTICIPANTS,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SCREEN_X,
    SCREEN_Y,
    SEAT_CENTERS_X,
    STUDIO_URI,
    _character_layout,
    _master_path,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_EPISODE_ID = "cc1ad449-9cad-4a40-a150-652db0b7dc7a"
PRIMER_ASSET_ID = "9e052a27-301c-4bb5-a0d3-d5eec44f1238"
SUBTITLE_ASSET_ID = "7460ebb8-56df-4d13-8ca7-1e333f77fc4c"
OUTPUT_ROOT = ROOT / "output/production-v2/full-production/render"
ANIMATION_MANIFEST = (
    ROOT / "output/production-v2/full-production/animation/manifest.json"
)
BROLL_ROOT = (
    ROOT
    / "storage/object-store/dialecticore/episodes"
    / SOURCE_EPISODE_ID
    / "opening-media"
)
BROLL_FILES = (
    "4be330caeefbaaea.mp4",
    "5da7840655cc372f.mp4",
    "b196f11bebc88fe8.mp4",
    "b46ce26dc4bff803.mp4",
    "f114ca13e2def8b9.mp4",
)
BROLL_SOURCE_IN_SECONDS = (24, 18, 32, 12, 20)
BROLL_CLIP_SECONDS = 62.0
BROLL_CROSSFADE_SECONDS = 1.5
PRESENTATION_TRANSITION_SECONDS = 2.0
FPS = 24


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-4000:]}"
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _segment_fingerprint(
    record: dict[str, Any], *, studio: Path, reel: Path
) -> str:
    payload = {
        "render_policy": "production_v2_full_turn.v1",
        "participant_id": record["participant_id"],
        "duration_ms": record["duration_ms"],
        "animation_sha256": record["artifact"]["downloaded_sha256"],
        "audio_sha256": record["audio_sha256"],
        "studio_sha256": _sha256(studio),
        "reel_sha256": _sha256(reel),
        "presentation_mode": _presentation_mode(int(record["index"])),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _segment_is_current(
    record: dict[str, Any], *, studio: Path, reel: Path, output: Path
) -> bool:
    sidecar = output.with_suffix(".fingerprint")
    if not output.is_file() or not sidecar.is_file():
        return False
    return sidecar.read_text().strip() == _segment_fingerprint(
        record, studio=studio, reel=reel
    )


def _object_path(uri: str) -> Path:
    prefix = "object://dialecticore/"
    if not uri.startswith(prefix):
        raise RuntimeError(f"unsupported object URI: {uri}")
    return ROOT / "storage/object-store/dialecticore" / uri.removeprefix(prefix)


def _source_episode() -> dict[str, Any]:
    url = f"http://127.0.0.1:8000/api/v1/episodes/{SOURCE_EPISODE_ID}"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        return json.load(response)


def _download_studio(path: Path) -> None:
    if path.is_file():
        return
    query = urllib.parse.urlencode({"uri": STUDIO_URI})
    url = f"http://127.0.0.1:8000/api/v1/show-media/scene-reference-image/download?{query}"
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        path.write_bytes(response.read())


def _probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _broll_clip_records(sources: list[Path]) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    step = BROLL_CLIP_SECONDS - BROLL_CROSSFADE_SECONDS
    for index, (source_in, source) in enumerate(
        zip(BROLL_SOURCE_IN_SECONDS, sources, strict=True)
    ):
        clips.append(
            {
                "index": index + 1,
                "path": str(source.relative_to(ROOT)),
                "sha256": _sha256(source),
                "timeline_start_ms": round(index * step * 1000),
                "timeline_end_ms": round((index * step + BROLL_CLIP_SECONDS) * 1000),
                "source_in_ms": source_in * 1000,
                "crossfade_ms": round(BROLL_CROSSFADE_SECONDS * 1000),
            }
        )
    return clips


def _build_broll_reel(output: Path) -> list[dict[str, Any]]:
    sources = [BROLL_ROOT / name for name in BROLL_FILES]
    for source in sources:
        if not source.is_file():
            raise RuntimeError(f"B-roll source is missing: {source}")
    clips = _broll_clip_records(sources)
    source_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "policy": "production_v2_broll_reel.v1",
                "clips": clips,
                "duration_seconds": BROLL_CLIP_SECONDS,
                "crossfade_seconds": BROLL_CROSSFADE_SECONDS,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    sidecar = output.with_suffix(".fingerprint")
    if output.is_file() and sidecar.is_file() and sidecar.read_text().strip() == source_fingerprint:
        return clips
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for source_in, source in zip(BROLL_SOURCE_IN_SECONDS, sources, strict=True):
        command.extend(
            ["-ss", str(source_in), "-t", str(BROLL_CLIP_SECONDS), "-i", str(source)]
        )
    filters: list[str] = []
    for index in range(len(sources)):
        filters.append(
            f"[{index}:v]scale=1280:720:force_original_aspect_ratio=increase,"
            f"crop=1280:720,fps={FPS},setsar=1,setpts=PTS-STARTPTS[v{index}]"
        )
    previous = "v0"
    step = BROLL_CLIP_SECONDS - BROLL_CROSSFADE_SECONDS
    for index in range(len(sources)):
        if index == 0:
            continue
        output_label = f"xf{index}"
        offset = index * step
        filters.append(
            f"[{previous}][v{index}]xfade=transition=fade:"
            f"duration={BROLL_CROSSFADE_SECONDS}:offset={offset}[{output_label}]"
        )
        previous = output_label
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{previous}]",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run(command)
    sidecar.write_text(source_fingerprint + "\n")
    return clips


def _presentation_mode(turn_index: int) -> str:
    # Three deliberately sparse explainer treatments; all other dialogue keeps
    # the active speaker prominent in the studio camera.
    return {
        1: "roundtrip",
        9: "enter",
        10: "exit",
        14: "roundtrip",
    }.get(turn_index, "rear_screen")


def _presentation_blend(mode: str, duration_ms: int) -> str:
    duration_frames = max(1, round(duration_ms * FPS / 1000))
    transition_frames = round(PRESENTATION_TRANSITION_SECONDS * FPS)

    def eased(start: int) -> str:
        return f"(0.5-0.5*cos(PI*(N-{start})/{transition_frames}))"

    if mode == "fullscreen":
        return "B"
    if mode == "enter":
        start = FPS
        end = start + transition_frames
        progress = eased(start)
        return f"if(lt(N,{start}),A,if(lt(N,{end}),A*(1-{progress})+B*{progress},B))"
    if mode == "exit":
        start = max(0, duration_frames - transition_frames - FPS)
        end = start + transition_frames
        progress = eased(start)
        return f"if(lt(N,{start}),B,if(lt(N,{end}),B*(1-{progress})+A*{progress},A))"
    if mode == "roundtrip" and duration_frames > transition_frames * 2 + FPS * 2:
        enter_start = FPS
        enter_end = enter_start + transition_frames
        exit_start = duration_frames - transition_frames - FPS
        exit_end = exit_start + transition_frames
        enter = eased(enter_start)
        exit_progress = eased(exit_start)
        return (
            f"if(lt(N,{enter_start}),A,"
            f"if(lt(N,{enter_end}),A*(1-{enter})+B*{enter},"
            f"if(lt(N,{exit_start}),B,"
            f"if(lt(N,{exit_end}),B*(1-{exit_progress})+A*{exit_progress},A))))"
        )
    return "A"


def _render_turn(
    *, record: dict[str, Any], studio: Path, reel: Path, start_ms: int, output: Path
) -> None:
    participant_id = record["participant_id"]
    duration_ms = int(record["duration_ms"])
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    command.extend(["-loop", "1", "-i", str(studio)])
    command.extend(["-ss", f"{start_ms / 1000:.3f}", "-i", str(reel)])
    for seated_id in PARTICIPANTS:
        source = (
            ROOT / record["artifact_path"]
            if seated_id == participant_id
            else _master_path(seated_id)
        )
        if source.suffix == ".mp4":
            command.extend(["-i", str(source)])
        else:
            command.extend(["-loop", "1", "-i", str(source)])
        command.extend(["-loop", "1", "-i", str(_master_path(seated_id))])
    audio_input_index = 2 + len(PARTICIPANTS) * 2
    command.extend(["-i", str(ROOT / record["audio_path"])])

    filters = [
        "[0:v]scale=1672:941,format=rgba[studio]",
        (
            f"[1:v]scale={SCREEN_WIDTH}:{SCREEN_HEIGHT}:"
            "force_original_aspect_ratio=increase,"
            f"crop={SCREEN_WIDTH}:{SCREEN_HEIGHT},fps={FPS},"
            "setpts=PTS-STARTPTS,format=rgba[screen]"
        ),
        f"[studio][screen]overlay={SCREEN_X}:{SCREEN_Y}:format=auto[screenbase]",
    ]
    previous = "screenbase"
    input_index = 2
    for seat_index, seated_id in enumerate(PARTICIPANTS):
        source_index = input_index
        matte_index = input_index + 1
        layout = _character_layout(seated_id)
        size = layout["canvas_size"]
        if seated_id == participant_id:
            filters.extend(
                [
                    f"[{source_index}:v]scale={size}:{size}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2,format=rgb24[active_rgb]",
                    f"[{matte_index}:v]alphaextract,scale={size}:{size}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=black[active_alpha]",
                    "[active_rgb][active_alpha]alphamerge[character]",
                ]
            )
        else:
            filters.append(
                f"[{source_index}:v]scale={size}:{size}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                "eq=brightness=-0.12:saturation=0.55,format=rgba[character]"
            )
        next_label = f"people{seat_index}"
        filters.append(
            f"[{previous}][character]overlay={layout['left']}:{layout['top']}:"
            f"format=auto[{next_label}]"
        )
        previous = next_label
        input_index += 2
    filters.extend(
        [
            f"[0:v]crop=1672:{941 - DESK_TOP}:0:{DESK_TOP},format=rgba[desk]",
            f"[{previous}][desk]overlay=0:{DESK_TOP}:format=auto[studio_people]",
            (
                f"[studio_people]pad={1672 + CAMERA_EDGE_EXTENSION * 2}:941:"
                f"{CAMERA_EDGE_EXTENSION}:0:color=0x070b23[extended_studio]"
            ),
            (
                f"[extended_studio]crop={CAMERA_WIDTH}:{CAMERA_HEIGHT}:"
                f"{SEAT_CENTERS_X[PARTICIPANTS.index(participant_id)]}:{CAMERA_TOP},"
                "scale=1280:720:flags=lanczos,setsar=1,"
                f"setpts=N/({FPS}*TB)[studio_camera]"
            ),
            (
                "[1:v]scale=1280:720:force_original_aspect_ratio=increase,"
                f"crop=1280:720,fps={FPS},setsar=1,setpts=N/({FPS}*TB)[fullscreen]"
            ),
        ]
    )
    mode = _presentation_mode(int(record["index"]))
    blend = _presentation_blend(mode, duration_ms)
    filters.append(
        f"[studio_camera][fullscreen]blend=all_expr='{blend}',"
        f"fps={FPS},format=yuv420p[vout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            f"{audio_input_index}:a:0",
            "-t",
            f"{duration_ms / 1000:.3f}",
            "-r",
            str(FPS),
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
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run(command)


def _concat_files(
    paths: list[Path], output: Path, *, copy: bool, duration_ms: int
) -> None:
    concat = output.with_suffix(".concat.txt")
    concat.write_text(
        "".join(f"file '{path.resolve().as_posix()}'\n" for path in paths)
    )
    command = [
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
    ]
    if copy:
        command.extend(["-c", "copy"])
    else:
        command.extend(
            [
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
                "-ar",
                "48000",
            ]
        )
    command.extend(
        ["-t", f"{duration_ms / 1000:.3f}", "-movflags", "+faststart", str(output)]
    )
    _run(command)


VTT_TIMESTAMP = re.compile(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")


def _shift_vtt(source: Path, output: Path, offset_ms: int) -> None:
    def replace(match: re.Match[str]) -> str:
        total = (
            int(match["h"]) * 3_600_000
            + int(match["m"]) * 60_000
            + int(match["s"]) * 1000
            + int(match["ms"])
            + offset_ms
        )
        hours, remainder = divmod(total, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    output.write_text(VTT_TIMESTAMP.sub(replace, source.read_text()))


def _timeline(
    jobs: list[dict[str, Any]],
    *,
    primer_duration_ms: int,
    broll_clips: list[dict[str, Any]],
) -> dict[str, Any]:
    tracks: dict[str, list[dict[str, Any]]] = {
        "dialogue": [],
        "character_performance": [],
        "camera_direction": [],
        "broll_content": [],
        "broll_presentation": [],
        "captions": [],
    }
    segments: list[dict[str, Any]] = []
    discussion_offset = 0
    for record in jobs:
        start_ms = primer_duration_ms + discussion_offset
        end_ms = start_ms + int(record["duration_ms"])
        clip_id = f"turn-{int(record['index']):02d}"
        base = {
            "id": clip_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "source_in_ms": 0,
            "source_out_ms": int(record["duration_ms"]),
        }
        tracks["dialogue"].append(
            {
                **base,
                "turn_id": record["turn_id"],
                "participant_id": record["participant_id"],
                "audio_asset_id": record["audio_asset_id"],
                "text": record["text"],
            }
        )
        tracks["character_performance"].append(
            {
                **base,
                "participant_id": record["participant_id"],
                "animation_job_id": record["job_id"],
                "animation_path": record["artifact_path"],
            }
        )
        tracks["camera_direction"].append(
            {
                **base,
                "participant_id": record["participant_id"],
                "framing": "speaker_centered_with_neighbors",
                "speaker_center_percent": 50,
                "crop": {
                    "x": SEAT_CENTERS_X[PARTICIPANTS.index(record["participant_id"])],
                    "y": CAMERA_TOP,
                    "width": CAMERA_WIDTH,
                    "height": CAMERA_HEIGHT,
                },
            }
        )
        mode = _presentation_mode(int(record["index"]))
        tracks["broll_presentation"].append(
            {
                **base,
                "mode": mode,
                "transition_duration_ms": round(PRESENTATION_TRANSITION_SECONDS * 1000),
                "easing": "ease_in_out_cosine",
                "audio_mode": "muted",
            }
        )
        segments.append(
            {
                "id": clip_id,
                "start_ms": start_ms,
                "duration_ms": int(record["duration_ms"]),
                "speaker_participant_id": record["participant_id"],
                "transcript_turn_id": record["turn_id"],
                "camera": tracks["camera_direction"][-1],
            }
        )
        discussion_offset += int(record["duration_ms"])
    for clip in broll_clips:
        tracks["broll_content"].append(
            {
                "id": f"broll-{clip['index']:02d}",
                "start_ms": primer_duration_ms + clip["timeline_start_ms"],
                "end_ms": primer_duration_ms + clip["timeline_end_ms"],
                "source_in_ms": clip["source_in_ms"],
                "source_out_ms": clip["source_in_ms"] + round(BROLL_CLIP_SECONDS * 1000),
                "source_path": clip["path"],
                "source_sha256": clip["sha256"],
                "crossfade_ms": clip["crossfade_ms"],
                "audio_mode": "muted",
                "provenance_required": False,
            }
        )
    tracks["captions"].append(
        {
            "id": "captions-de",
            "start_ms": primer_duration_ms,
            "end_ms": primer_duration_ms + discussion_offset,
            "language": "de",
            "source_asset_id": SUBTITLE_ASSET_ID,
            "offset_ms": primer_duration_ms,
        }
    )
    return {
        "schema_version": "episode_timeline.v3",
        "source_episode_id": SOURCE_EPISODE_ID,
        "duration_ms": primer_duration_ms + discussion_offset,
        "primer": {"asset_id": PRIMER_ASSET_ID, "duration_ms": primer_duration_ms},
        "segments": segments,
        "tracks": tracks,
        "render_contract": {
            "broll_source_clock_continuous": True,
            "broll_audio_default": "muted",
            "speaker_center_percent_range": [45, 55],
            "desk_foreground_occlusion_y": DESK_TOP,
        },
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    episode = _source_episode()
    animation = json.loads(ANIMATION_MANIFEST.read_text())
    jobs = sorted(animation["jobs"], key=lambda item: int(item["index"]))
    if len(jobs) != 21 or any(job.get("state") != "completed" for job in jobs):
        states: dict[str, int] = {}
        for job in jobs:
            state = str(job.get("state") or "unknown")
            states[state] = states.get(state, 0) + 1
        raise RuntimeError(f"animation batch is not complete: {states}")
    for job in jobs:
        artifact = ROOT / job["artifact_path"]
        if not artifact.is_file() or _sha256(artifact) != job["artifact"]["downloaded_sha256"]:
            raise RuntimeError(f"animation artifact failed integrity check: {artifact}")

    assets = {asset["id"]: asset for asset in episode["assets"]}
    primer_asset = assets[PRIMER_ASSET_ID]
    subtitle_asset = assets[SUBTITLE_ASSET_ID]
    primer = _object_path(primer_asset["storage_uri"])
    subtitle = _object_path(subtitle_asset["storage_uri"])
    primer_duration_ms = int(primer_asset["duration_ms"])
    studio = OUTPUT_ROOT / "studio-reference.png"
    _download_studio(studio)
    reel = OUTPUT_ROOT / "broll-reel.mp4"
    broll_clips = _build_broll_reel(reel)

    segments_dir = OUTPUT_ROOT / "segments"
    segments_dir.mkdir(exist_ok=True)
    discussion_paths: list[Path] = []
    discussion_offset = 0
    for job in jobs:
        segment = segments_dir / f"{int(job['index']):02d}-{job['participant_id']}.mp4"
        if _segment_is_current(job, studio=studio, reel=reel, output=segment):
            print(f"reused {int(job['index']):02d}/21 {job['participant_id']}", flush=True)
        else:
            _render_turn(
                record=job,
                studio=studio,
                reel=reel,
                start_ms=discussion_offset,
                output=segment,
            )
            segment.with_suffix(".fingerprint").write_text(
                _segment_fingerprint(job, studio=studio, reel=reel) + "\n"
            )
            print(
                f"rendered {int(job['index']):02d}/21 {job['participant_id']}",
                flush=True,
            )
        discussion_paths.append(segment)
        discussion_offset += int(job["duration_ms"])
    discussion = OUTPUT_ROOT / "discussion.mp4"
    _concat_files(
        discussion_paths,
        discussion,
        copy=True,
        duration_ms=discussion_offset,
    )
    preview = OUTPUT_ROOT / "production-v2-full-preview.mp4"
    _concat_files(
        [primer, discussion],
        preview,
        copy=False,
        duration_ms=primer_duration_ms + discussion_offset,
    )

    timeline = _timeline(
        jobs,
        primer_duration_ms=primer_duration_ms,
        broll_clips=broll_clips,
    )
    timeline_path = OUTPUT_ROOT / "timeline-v3.json"
    timeline_path.write_text(json.dumps(timeline, indent=2, sort_keys=True) + "\n")
    shifted_subtitle = OUTPUT_ROOT / "production-v2-full-preview.de.vtt"
    _shift_vtt(subtitle, shifted_subtitle, primer_duration_ms)
    manifest = {
        "schema_version": "dialecticore.production_v2.full_render.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "source_episode_id": SOURCE_EPISODE_ID,
        "animation_manifest": str(ANIMATION_MANIFEST.relative_to(ROOT)),
        "primer": {
            "asset_id": PRIMER_ASSET_ID,
            "path": str(primer.relative_to(ROOT)),
            "sha256": _sha256(primer),
            "duration_ms": primer_duration_ms,
        },
        "broll_reel": {
            "path": str(reel.relative_to(ROOT)),
            "sha256": _sha256(reel),
            "clips": broll_clips,
            "audio_mode": "muted",
        },
        "timeline": {
            "path": str(timeline_path.relative_to(ROOT)),
            "sha256": _sha256(timeline_path),
            "duration_ms": timeline["duration_ms"],
        },
        "subtitle": {
            "path": str(shifted_subtitle.relative_to(ROOT)),
            "sha256": _sha256(shifted_subtitle),
            "offset_ms": primer_duration_ms,
        },
        "discussion": {
            "path": str(discussion.relative_to(ROOT)),
            "sha256": _sha256(discussion),
            "probe": _probe(discussion),
        },
        "preview": {
            "path": str(preview.relative_to(ROOT)),
            "sha256": _sha256(preview),
            "bytes": preview.stat().st_size,
            "probe": _probe(preview),
        },
    }
    manifest_path = OUTPUT_ROOT / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest["preview"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
