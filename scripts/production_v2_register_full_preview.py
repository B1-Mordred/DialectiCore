#!/usr/bin/env python3
"""Register the complete production-v2 preview as a separate review episode."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.domain.enums import (  # noqa: E402
    AssetType,
    EpisodeStatus,
    QualitySeverity,
)
from app.domain.schemas import (  # noqa: E402
    Approval,
    Asset,
    AuditEvent,
    EpisodeCreateRequest,
    QualityResult,
    TranscriptTurn,
    TranscriptVersion,
)
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository  # noqa: E402
from app.services.object_storage import create_object_store  # noqa: E402

SOURCE_EPISODE_ID = UUID("cc1ad449-9cad-4a40-a150-652db0b7dc7a")
OUTPUT_ROOT = ROOT / "output/production-v2/full-production/render"
ANIMATION_MANIFEST = ROOT / "output/production-v2/full-production/animation/manifest.json"
QC_PATH = OUTPUT_ROOT / "qc.json"
MARKER = "dialecticore.production_v2.full_preview.v1"


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,start_time,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(completed.stdout)
    streams = raw.get("streams") or []
    video = next(item for item in streams if item.get("codec_type") == "video")
    audio = next(item for item in streams if item.get("codec_type") == "audio")
    numerator, denominator = str(video["r_frame_rate"]).split("/", 1)
    video_start = float(video.get("start_time") or 0)
    audio_start = float(audio.get("start_time") or 0)
    return {
        "probe_tool": "ffprobe",
        "probe_warnings": [],
        "duration_ms": round(float(raw["format"]["duration"]) * 1000),
        "size_bytes": int(raw["format"]["size"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": float(numerator) / float(denominator),
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
        "audio_sample_rate": int(audio["sample_rate"]),
        "audio_channels": int(audio["channels"]),
        "av_offset_ms": round(abs(video_start - audio_start) * 1000, 3),
    }


def _remap_timeline(
    source: dict[str, object],
    *,
    turn_ids: dict[str, str],
    audio_ids: dict[str, str],
    speaking_ids: dict[str, str],
) -> dict[str, object]:
    timeline = copy.deepcopy(source)
    tracks = timeline["tracks"]
    for clip in tracks["dialogue"]:
        clip["turn_id"] = turn_ids[clip["turn_id"]]
        clip["audio_asset_id"] = audio_ids[clip["audio_asset_id"]]
        clip["asset_id"] = clip["audio_asset_id"]
    for clip in tracks["character_performance"]:
        source_turn_id = clip["id"].removeprefix("turn-")
        clip["speaking_asset_id"] = speaking_ids[source_turn_id]
        clip["asset_id"] = clip["speaking_asset_id"]
    dialogue_by_id = {clip["id"]: clip for clip in tracks["dialogue"]}
    character_by_id = {clip["id"]: clip for clip in tracks["character_performance"]}
    for segment in timeline["segments"]:
        segment["transcript_turn_id"] = turn_ids[segment["transcript_turn_id"]]
        segment["source_turn_id"] = segment["transcript_turn_id"]
        segment["audio_asset_id"] = dialogue_by_id[segment["id"]]["asset_id"]
        segment["video_asset_id"] = character_by_id[segment["id"]]["asset_id"]
    return timeline


def main() -> int:
    settings = Settings()
    engine = create_database_engine(settings)
    initialize_database(engine)
    repo = EpisodeRepository(create_session_factory(engine))
    source = repo.get(SOURCE_EPISODE_ID)
    render_manifest = json.loads((OUTPUT_ROOT / "manifest.json").read_text())
    animation = json.loads(ANIMATION_MANIFEST.read_text())
    qc_result = json.loads(QC_PATH.read_text())
    if qc_result.get("status") != "pass":
        raise RuntimeError(
            f"Production v2 technical QC does not pass: {qc_result.get('failed_checks')}"
        )
    preview_path = ROOT / render_manifest["preview"]["path"]
    preview_probe = _probe(preview_path)
    preview_checksum = f"sha256:{render_manifest['preview']['sha256']}"

    existing_summary = next(
        (
            item
            for item in repo.list_compact()
            if item.title == f"{source.definition.title} — Production v2"
        ),
        None,
    )
    revision = 1
    is_revision = False
    prior_render_asset_id: str | None = None
    prior_timeline_asset_id: str | None = None
    prior_approval_id: str | None = None
    if existing_summary is not None:
        episode = repo.get(existing_summary.id)
        control = episode.workflow_control.get("production_v2_full_preview", {})
        if control.get("schema_version") == MARKER:
            prior = next(
                (
                    asset
                    for asset in episode.assets
                    if str(asset.id) == control.get("render_asset_id")
                ),
                None,
            )
            prior_manifest = (
                prior.generation_metadata.get("render_manifest")
                if prior is not None
                and isinstance(prior.generation_metadata.get("render_manifest"), dict)
                else {}
            )
            prior_timeline = (
                prior_manifest.get("timeline")
                if isinstance(prior_manifest.get("timeline"), dict)
                else {}
            )
            if (
                prior is not None
                and prior.checksum == preview_checksum
                and prior_timeline.get("sha256")
                == render_manifest.get("timeline", {}).get("sha256")
            ):
                print(
                    json.dumps(
                        {
                            "episode_id": str(episode.id),
                            "render_asset_id": str(prior.id),
                            "approval_id": control.get("approval_id"),
                            "created": False,
                        },
                        indent=2,
                    )
                )
                return 0
            if control.get("source_episode_id") != str(source.id):
                raise RuntimeError("existing Production v2 preview has a different source")
            revision = int(control.get("revision") or 1) + 1
            is_revision = True
            prior_render_asset_id = control.get("render_asset_id")
            prior_timeline_asset_id = control.get("timeline_asset_id")
            prior_approval_id = control.get("approval_id")
        elif episode.assets or episode.transcripts:
            raise RuntimeError("an existing Production v2 episode has incompatible content")
    else:
        definition = source.definition.model_copy(deep=True)
        definition.title = f"{source.definition.title} — Production v2"
        episode = repo.create(
            EpisodeCreateRequest(
                project_id=source.project_id,
                definition=definition,
                participants=source.participants,
                model_endpoints=source.model_endpoints,
            )
        )

    source_transcript = next(
        item for item in source.transcripts if item.id == source.canonical_transcript_version_id
    )
    if is_revision:
        transcript = next(
            item
            for item in episode.transcripts
            if item.id == episode.canonical_transcript_version_id
        )
        if len(transcript.turns) != len(source_transcript.turns):
            raise RuntimeError("registered Production v2 transcript no longer matches source")
        turn_ids = {
            str(source_turn.id): str(turn.id)
            for source_turn, turn in zip(source_transcript.turns, transcript.turns, strict=True)
        }
    else:
        transcript = TranscriptVersion(
            episode_id=episode.id,
            type=source_transcript.type,
            language=source_transcript.language,
            status="approved",
            semantic_fidelity_score=source_transcript.semantic_fidelity_score,
            localization_metadata={
                **source_transcript.localization_metadata,
                "source_episode_id": str(source.id),
                "source_transcript_version_id": str(source_transcript.id),
                "production_v2_copy": True,
            },
        )
        turn_ids = {}
        for source_turn in source_transcript.turns:
            turn = TranscriptTurn(
                transcript_version_id=transcript.id,
                source_discussion_turn_ids=source_turn.source_discussion_turn_ids,
                speaker_participant_id=source_turn.speaker_participant_id,
                turn_type=source_turn.turn_type,
                text=source_turn.text,
                edit_type=source_turn.edit_type,
                semantic_difference_score=source_turn.semantic_difference_score,
                claims=source_turn.claims,
                pronunciation_markup=source_turn.pronunciation_markup,
                status="approved",
            )
            transcript.turns.append(turn)
            turn_ids[str(source_turn.id)] = str(turn.id)
        episode.transcripts.append(transcript)
        episode.canonical_transcript_version_id = transcript.id

    object_store = create_object_store(settings)
    if is_revision:
        audio_ids = {
            str(asset.generation_metadata["source_asset_id"]): str(asset.id)
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.status == "completed"
            and asset.generation_metadata.get("source_asset_id")
        }
        speaking_by_turn = {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type == AssetType.video
            and asset.status == "completed"
            and asset.generation_metadata.get("visual_role") == "production_v2_speaking_character"
        }
        speaking_ids = {
            f"{int(job['index']):02d}": str(speaking_by_turn[turn_ids[job["turn_id"]]].id)
            for job in animation["jobs"]
        }
    else:
        source_audio_by_turn = {
            asset.source_entity_id: asset
            for asset in source.assets
            if asset.asset_type == AssetType.audio
            and asset.status == "completed"
            and asset.generation_metadata.get("transcript_version_id") == str(source_transcript.id)
        }
        audio_ids = {}
        for source_turn_id, new_turn_id in turn_ids.items():
            source_audio = source_audio_by_turn[source_turn_id]
            audio = Asset(
                episode_id=episode.id,
                asset_type=AssetType.audio,
                language=source_audio.language,
                source_entity_type="transcript_turn",
                source_entity_id=new_turn_id,
                storage_uri=source_audio.storage_uri,
                mime_type=source_audio.mime_type,
                duration_ms=source_audio.duration_ms,
                checksum=source_audio.checksum,
                status="completed",
                generation_metadata={
                    **source_audio.generation_metadata,
                    "transcript_version_id": str(transcript.id),
                    "source_episode_id": str(source.id),
                    "source_asset_id": str(source_audio.id),
                    "shared_immutable_audio": True,
                },
            )
            episode.assets.append(audio)
            audio_ids[str(source_audio.id)] = str(audio.id)

        speaking_ids = {}
        for job in animation["jobs"]:
            source_path = ROOT / job["artifact_path"]
            stored = object_store.put_bytes(
                key=(
                    f"production-v2/full/{episode.id}/speaking/"
                    f"{int(job['index']):02d}-{job['participant_id']}.mp4"
                ),
                payload=source_path.read_bytes(),
                content_type="video/mp4",
            )
            speaking = Asset(
                episode_id=episode.id,
                asset_type=AssetType.video,
                source_entity_type="transcript_turn",
                source_entity_id=turn_ids[job["turn_id"]],
                storage_uri=stored.uri,
                mime_type=stored.content_type,
                duration_ms=int(job["duration_ms"]),
                width=1024,
                height=1024,
                fps=12,
                checksum=stored.checksum,
                status="completed",
                generation_metadata={
                    "visual_role": "production_v2_speaking_character",
                    "participant_id": job["participant_id"],
                    "b1_job_id": job["job_id"],
                    "b1_runtime": job.get("runtime"),
                    "b1_telemetry": job.get("telemetry"),
                    "source_audio_asset_id": job["audio_asset_id"],
                    "source_audio_sha256": job["source_audio_sha256"],
                    "upload_audio_sha256": job["audio_sha256"],
                    "animation_input_policy": (
                        "detector_source_crop"
                        if job["participant_id"] == "deepseek"
                        else "normalized_seated_master"
                    ),
                    "object_storage_path": str(stored.path),
                    "scheduler_managed": True,
                },
            )
            episode.assets.append(speaking)
            speaking_ids[f"{int(job['index']):02d}"] = str(speaking.id)

    source_timeline = json.loads((ROOT / render_manifest["timeline"]["path"]).read_text())
    timeline = _remap_timeline(
        source_timeline,
        turn_ids=turn_ids,
        audio_ids=audio_ids,
        speaking_ids=speaking_ids,
    )
    timeline.update(
        {
            "id": str(uuid4()),
            "episode_id": str(episode.id),
            "transcript_version_id": str(transcript.id),
            "language": transcript.language,
            "editable": True,
            "edit_version": revision,
        }
    )
    existing_broll_by_source_id = {
        str(asset.generation_metadata.get("source_asset_id")): asset
        for asset in episode.assets
        if asset.asset_type == AssetType.broll
        and asset.status == "completed"
        and asset.generation_metadata.get("source_asset_id")
    }
    source_broll_by_name = {
        Path(asset.storage_uri or "").name: asset
        for asset in source.assets
        if asset.status == "completed"
        and asset.asset_type in {AssetType.video, AssetType.broll}
        and asset.storage_uri
    }
    for clip in timeline.get("tracks", {}).get("broll_content", []):
        source_path = str(clip.get("source_path") or "")
        source_broll = source_broll_by_name.get(Path(source_path).name)
        if source_broll is None:
            continue
        broll_asset = existing_broll_by_source_id.get(str(source_broll.id))
        if broll_asset is None:
            broll_asset = Asset(
                episode_id=episode.id,
                asset_type=AssetType.broll,
                source_entity_type="production_v2_broll_source",
                source_entity_id=str(source.id),
                storage_uri=source_broll.storage_uri,
                mime_type=source_broll.mime_type,
                duration_ms=source_broll.duration_ms,
                width=source_broll.width,
                height=source_broll.height,
                fps=source_broll.fps,
                checksum=source_broll.checksum,
                status="completed",
                generation_metadata={
                    **source_broll.generation_metadata,
                    "source_episode_id": str(source.id),
                    "source_asset_id": str(source_broll.id),
                    "shared_immutable_broll": True,
                },
            )
            episode.assets.append(broll_asset)
            existing_broll_by_source_id[str(source_broll.id)] = broll_asset
        clip["asset_id"] = str(broll_asset.id)
    timeline["duration_ms"] = int(preview_probe["duration_ms"])
    timeline_payload = json.dumps(timeline, indent=2, sort_keys=True).encode()
    revision_prefix = f"revision-{revision}/" if revision > 1 else ""
    stored_timeline = object_store.put_bytes(
        key=f"production-v2/full/{episode.id}/{revision_prefix}timeline-v3.json",
        payload=timeline_payload,
        content_type="application/vnd.dialecticore.timeline+json",
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri=stored_timeline.uri,
        mime_type=stored_timeline.content_type,
        duration_ms=int(preview_probe["duration_ms"]),
        checksum=stored_timeline.checksum,
        status="completed",
        generation_metadata={
            "timeline_json": timeline,
            "schema_version": timeline["schema_version"],
            "composition_policy": "studio_camera_cuts.v2",
            "revision": revision,
            "source_timeline_sha256": render_manifest["timeline"]["sha256"],
            "object_storage_path": str(stored_timeline.path),
        },
    )
    episode.assets.append(timeline_asset)

    if is_revision:
        subtitle_asset = next(
            asset for asset in episode.assets if str(asset.id) == control.get("subtitle_asset_id")
        )
    else:
        subtitle_path = ROOT / render_manifest["subtitle"]["path"]
        stored_subtitle = object_store.put_bytes(
            key=f"production-v2/full/{episode.id}/subtitles/de.vtt",
            payload=subtitle_path.read_bytes(),
            content_type="text/vtt",
        )
        subtitle_asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.subtitle,
            language="de",
            source_entity_type="transcript_version",
            source_entity_id=str(transcript.id),
            storage_uri=stored_subtitle.uri,
            mime_type=stored_subtitle.content_type,
            duration_ms=int(preview_probe["duration_ms"]),
            checksum=stored_subtitle.checksum,
            status="completed",
            generation_metadata={
                "format": "vtt",
                "transcript_version_id": str(transcript.id),
                "timeline_offset_ms": render_manifest["subtitle"]["offset_ms"],
                "object_storage_path": str(stored_subtitle.path),
            },
        )
        episode.assets.append(subtitle_asset)

    if prior_timeline_asset_id:
        prior_timeline = next(
            asset for asset in episode.assets if str(asset.id) == prior_timeline_asset_id
        )
        prior_timeline.status = "replaced"
        prior_timeline.updated_at = datetime.now(UTC)
    if prior_render_asset_id:
        prior_render = next(
            asset for asset in episode.assets if str(asset.id) == prior_render_asset_id
        )
        prior_render.status = "replaced"
        prior_render.generation_metadata["approval_status"] = "superseded"
        prior_render.generation_metadata["superseded_by_revision"] = revision
        prior_render.updated_at = datetime.now(UTC)
    if prior_approval_id:
        prior_approval = next(
            approval for approval in episode.approvals if str(approval.id) == prior_approval_id
        )
        if prior_approval.decision == "pending":
            prior_approval.decision = "rejected"
            prior_approval.comment = (
                "Superseded after human review: keep outer torsos behind the desk and "
                "use total-studio views for the participant introduction and conclusion."
            )
            prior_approval.user_id = "codex"

    stored_preview = object_store.put_bytes(
        key=f"production-v2/full/{episode.id}/{revision_prefix}preview.mp4",
        payload=preview_path.read_bytes(),
        content_type="video/mp4",
    )
    preview_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="de",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri=stored_preview.uri,
        mime_type=stored_preview.content_type,
        duration_ms=int(preview_probe["duration_ms"]),
        width=int(preview_probe["width"]),
        height=int(preview_probe["height"]),
        fps=float(preview_probe["fps"]),
        checksum=stored_preview.checksum,
        status="completed",
        generation_metadata={
            "render_type": "preview",
            "review_scope": "full_timeline",
            "composition_policy": "studio_camera_cuts.v2",
            "revision": revision,
            "production_v2": True,
            "render_manifest": render_manifest,
            "media_probe": preview_probe,
            "caption_track_asset_id": str(subtitle_asset.id),
            "caption_track_mode": "selectable",
            "approval_status": "pending",
            "object_storage_path": str(stored_preview.path),
            "render_ready": True,
        },
    )
    episode.assets.append(preview_asset)
    qc = QualityResult(
        episode_id=episode.id,
        target_type="render_asset",
        target_id=str(preview_asset.id),
        check_type="render_preview_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
        details={
            "technical_qc": qc_result,
            "full_timeline": True,
            "av_offset_ms": preview_probe["av_offset_ms"],
            "expected_duration_ms": timeline["duration_ms"],
            "actual_duration_ms": preview_probe["duration_ms"],
            "animation_turn_count": len(animation["jobs"]),
            "failed_animation_count": 0,
            "broll_source_count": len(render_manifest["broll_reel"]["clips"]),
            "source_v1_modified": False,
        },
    )
    episode.quality_results.append(qc)
    approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(preview_asset.id),
        comment=(
            "Review the complete Production v2 episode: character scale/contact, lip and "
            "head motion, speaker-centered cameras, continuous rear-screen video, eased "
            "fullscreen round trips, complete dialogue audio, and selectable captions."
        ),
    )
    episode.approvals.append(approval)
    preview_asset.generation_metadata["approval_id"] = str(approval.id)
    episode.workflow_control["production_v2_full_preview"] = {
        "schema_version": MARKER,
        "source_episode_id": str(source.id),
        "source_transcript_version_id": str(source_transcript.id),
        "transcript_version_id": str(transcript.id),
        "timeline_asset_id": str(timeline_asset.id),
        "subtitle_asset_id": str(subtitle_asset.id),
        "render_asset_id": str(preview_asset.id),
        "approval_id": str(approval.id),
        "status": "pending_review",
        "revision": revision,
        "supersedes_render_asset_id": prior_render_asset_id,
        "supersedes_timeline_asset_id": prior_timeline_asset_id,
        "supersedes_approval_id": prior_approval_id,
        "v1_assets_modified": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    episode.status = EpisodeStatus.ready
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="production_v2.full_preview.registered",
            actor="codex",
            details={
                "source_episode_id": str(source.id),
                "render_asset_id": str(preview_asset.id),
                "render_checksum": preview_asset.checksum,
                "timeline_asset_id": str(timeline_asset.id),
                "subtitle_asset_id": str(subtitle_asset.id),
                "animation_turn_count": len(animation["jobs"]),
                "managed_b1_jobs": True,
                "v1_assets_modified": False,
                "revision": revision,
                "supersedes_render_asset_id": prior_render_asset_id,
                "review_corrections": [
                    "outer_seats_inset_over_desk",
                    "opening_total_studio_view",
                    "conclusion_total_studio_view",
                ],
            },
        )
    )
    episode.updated_at = datetime.now(UTC)
    repo.save(episode)
    print(
        json.dumps(
            {
                "episode_id": str(episode.id),
                "render_asset_id": str(preview_asset.id),
                "approval_id": str(approval.id),
                "preview_checksum": preview_asset.checksum,
                "revision": revision,
                "created": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
