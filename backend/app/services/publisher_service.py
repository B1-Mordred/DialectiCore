from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
from uuid import UUID, uuid4

import httpx
from app.core.credentials import credential_reference_scheme
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity
from app.domain.schemas import (
    Asset,
    AuditEvent,
    Episode,
    PublisherTarget,
    PublishJob,
    PublishRequest,
    QualityResult,
)
from app.services.model_gateway import SecretResolver
from app.services.redaction import (
    is_sensitive_provider_response_key,
    safe_provider_response_payload,
)

YOUTUBE_API_BASE_URL = "https://www.googleapis.com"
YOUTUBE_UPLOAD_BASE_URL = "https://www.googleapis.com"


@dataclass(frozen=True)
class PublishAdapterResult:
    status: str
    remote_job_id: str | None
    publish_url: str | None
    metadata: dict


@dataclass(frozen=True)
class YouTubeOAuthAccess:
    access_token: str | None
    metadata: dict


class PublisherService:
    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport

    def publish_package(
        self,
        episode: Episode,
        request: PublishRequest,
        targets: list[PublisherTarget],
    ) -> Episode:
        target = self._target(request.publisher_target_id, targets)
        if not target.enabled:
            raise ValueError(f"publisher target {target.id} is disabled")
        package_asset = self._package_asset(episode, request.package_asset_id)
        live_delivery = not request.dry_run and target.adapter_type != "mock"
        self._ensure_package_qc_allows_publish(
            episode,
            package_asset,
            require_present=live_delivery,
        )
        production_manifest_asset = self._production_manifest_asset(
            episode,
            package_asset,
        )
        if live_delivery and production_manifest_asset is None:
            raise ValueError("production manifest is required before live publishing")
        if (
            live_delivery
            and production_manifest_asset is not None
            and not self._production_manifest_asset_valid(
                production_manifest_asset,
                package_asset,
            )
        ):
            raise ValueError(
                "valid production_manifest.v1 asset is required before live publishing"
            )
        existing = self._latest_publish_job(episode, target.id, package_asset.id)
        if existing is not None and existing.status != "replaced" and not request.regenerate:
            raise ValueError("publish job already exists for target package")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        now = datetime.now(UTC)
        job_id = uuid4()
        delivery_payload = self._delivery_payload(
            episode,
            package_asset,
            target,
            production_manifest_asset,
        )
        adapter_result = self._publish_with_adapter(
            job_id=job_id,
            target=target,
            package_asset=package_asset,
            delivery_payload=delivery_payload,
            dry_run=request.dry_run,
        )
        job = PublishJob(
            id=job_id,
            episode_id=episode.id,
            publisher_target_id=target.id,
            platform=target.platform,
            package_asset_id=package_asset.id,
            status=adapter_result.status,
            dry_run=request.dry_run or target.adapter_type == "mock",
            remote_job_id=adapter_result.remote_job_id,
            publish_url=adapter_result.publish_url,
            delivery_payload=delivery_payload,
            result_metadata={
                "adapter": target.adapter_type,
                "target_name": target.name,
                "package_checksum": package_asset.checksum,
                "package_storage_uri": package_asset.storage_uri,
                **adapter_result.metadata,
            },
            requested_at=now,
            completed_at=now if adapter_result.status == "completed" else None,
            updated_at=now,
        )
        episode.publish_jobs.append(job)
        qc = self._publish_qc(episode, job, package_asset, target)
        episode.quality_results.append(qc)
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="publisher.job.submitted",
                    actor=request.user_id or "system",
                    details={
                        "publish_job_id": str(job.id),
                        "publisher_target_id": target.id,
                        "platform": target.platform,
                        "package_asset_id": str(package_asset.id),
                        "dry_run": job.dry_run,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type=f"publisher.job.{job.status}",
                    actor=request.user_id or "system",
                    details={
                        "publish_job_id": str(job.id),
                        "publisher_target_id": target.id,
                        "platform": target.platform,
                        "package_asset_id": str(package_asset.id),
                        "publish_url": job.publish_url,
                        "status": job.status,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="publisher.job.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "publish_job_id": str(job.id),
                        "status": qc.status,
                        "failure_count": qc.details["failure_count"],
                        "warning_count": qc.details["warning_count"],
                    },
                ),
            ]
        )
        if not job.dry_run and job.status == "completed":
            episode.status = EpisodeStatus.completed
        episode.updated_at = now
        return episode

    def check_target_health(self, target: PublisherTarget) -> PublisherTarget:
        if target.adapter_type == "mock":
            return target.model_copy(
                update={
                    "health_status": "healthy",
                    "capabilities": {
                        **target.capabilities,
                        "dry_run": True,
                        "metadata_upload": True,
                    },
                }
            )
        if target.adapter_type == "youtube_resumable":
            return self._check_youtube_resumable_health(target)
        if not target.base_url:
            return target.model_copy(update={"health_status": "unconfigured"})
        if target.adapter_type not in {"http", "http_upload", "generic_http"}:
            return target.model_copy(update={"health_status": "unsupported"})

        capabilities = {
            **target.capabilities,
            "dry_run": True,
            "metadata_upload": True,
            "package_upload": target.capabilities.get("package_upload", True),
        }
        headers = self._request_headers(target)
        health_path = str(target.capabilities.get("health_path") or "/health")
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self._target_timeout(target),
                headers=headers,
            ) as client:
                response = client.get(f"{target.base_url.rstrip('/')}{health_path}")
            health_status = "healthy" if response.is_success else "unhealthy"
            if response.is_success:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if isinstance(payload, dict) and isinstance(payload.get("capabilities"), dict):
                    discovered = self._safe_response_payload(payload["capabilities"])
                    if isinstance(discovered, dict):
                        capabilities.update(discovered)
        except (httpx.HTTPError, RuntimeError) as exc:
            health_status = "unhealthy"
            capabilities["last_health_error"] = str(exc)
        return target.model_copy(
            update={"health_status": health_status, "capabilities": capabilities}
        )

    def _publish_with_adapter(
        self,
        job_id: UUID,
        target: PublisherTarget,
        package_asset: Asset,
        delivery_payload: dict,
        dry_run: bool,
    ) -> PublishAdapterResult:
        if target.adapter_type == "mock":
            remote_job_id = f"mock-{target.platform}-{str(job_id)[:8]}"
            return PublishAdapterResult(
                status="completed",
                remote_job_id=remote_job_id,
                publish_url=f"mock://{target.platform}/{remote_job_id}",
                metadata={
                    "dry_run_only": True,
                    "metadata_upload": True,
                    "video_upload": False,
                    "thumbnail_upload": False,
                    "subtitle_upload": False,
                },
            )
        if target.adapter_type == "youtube_resumable":
            if dry_run:
                return self._publish_youtube_resumable_dry_run(
                    job_id,
                    target,
                    package_asset,
                )
            return self._publish_youtube_resumable(
                target,
                package_asset,
                delivery_payload,
            )
        if dry_run:
            remote_job_id = f"dry-run-{target.platform}-{str(job_id)[:8]}"
            return PublishAdapterResult(
                status="completed",
                remote_job_id=remote_job_id,
                publish_url=f"dry-run://{target.platform}/{target.id}/{remote_job_id}",
                metadata={
                    "dry_run_only": True,
                    "request_endpoint": self._url_endpoint_evidence(
                        self._delivery_url(target)
                    ),
                    "would_upload_package": self._package_path(package_asset) is not None,
                },
            )
        if target.adapter_type in {"http", "http_upload", "generic_http"}:
            return self._publish_http(target, package_asset, delivery_payload)
        raise ValueError(f"unsupported publisher adapter: {target.adapter_type}")

    def _check_youtube_resumable_health(
        self,
        target: PublisherTarget,
    ) -> PublisherTarget:
        capabilities = {
            **target.capabilities,
            "dry_run": True,
            "metadata_upload": True,
            "video_upload": True,
            "resumable_upload": True,
            "thumbnail_upload": True,
            "subtitle_upload": True,
            "caption_upload": True,
            "oauth_required": True,
        }
        if not target.credential_reference and not self._youtube_refresh_configured(target):
            return target.model_copy(
                update={"health_status": "unconfigured", "capabilities": capabilities}
            )
        oauth_access = self._youtube_oauth_access(target)
        capabilities["oauth_access"] = oauth_access.metadata
        if not oauth_access.access_token:
            capabilities["last_health_error"] = oauth_access.metadata.get(
                "error",
                "youtube_oauth_access_token_unavailable",
            )
            return target.model_copy(
                update={"health_status": "unhealthy", "capabilities": capabilities}
            )

        url = self._youtube_health_url(target)
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self._target_timeout(target),
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {oauth_access.access_token}",
                },
            ) as client:
                response = client.get(url)
            health_status = "healthy" if response.is_success else "unhealthy"
            payload = self._response_payload(response)
            if isinstance(payload.get("capabilities"), dict):
                discovered = self._safe_response_payload(payload["capabilities"])
                if isinstance(discovered, dict):
                    capabilities.update(discovered)
            capabilities.update(
                {
                    "health_status_code": response.status_code,
                    "health_endpoint": self._url_endpoint_evidence(url),
                }
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            health_status = "unhealthy"
            capabilities["last_health_error"] = str(exc)
            capabilities["health_endpoint"] = self._url_endpoint_evidence(url)
        return target.model_copy(
            update={"health_status": health_status, "capabilities": capabilities}
        )

    def _publish_youtube_resumable_dry_run(
        self,
        job_id: UUID,
        target: PublisherTarget,
        package_asset: Asset,
    ) -> PublishAdapterResult:
        package_path = self._package_path(package_asset)
        package_entries = self._youtube_package_entry_names(package_path)
        remote_job_id = f"dry-run-youtube-resumable-{str(job_id)[:8]}"
        return PublishAdapterResult(
            status="completed",
            remote_job_id=remote_job_id,
            publish_url=f"dry-run://youtube/{target.id}/{remote_job_id}",
            metadata={
                "dry_run_only": True,
                "adapter_protocol": "youtube_data_api_resumable_upload",
                "initiate_upload_endpoint": self._url_endpoint_evidence(
                    self._youtube_resumable_session_url(target)
                ),
                "would_upload_video": any(entry.startswith("video/") for entry in package_entries),
                "would_upload_thumbnail": any(
                    entry.startswith("thumbnail/") for entry in package_entries
                ),
                "would_upload_subtitles": any(
                    entry.startswith("subtitles/") for entry in package_entries
                ),
                "thumbnail_upload_endpoint": self._url_endpoint_evidence(
                    self._youtube_thumbnail_upload_url(
                        target,
                        "{video_id}",
                    )
                ),
                "caption_upload_endpoint": self._url_endpoint_evidence(
                    self._youtube_caption_upload_url(target)
                ),
                "package_entry_count": len(package_entries),
            },
        )

    def _publish_youtube_resumable(
        self,
        target: PublisherTarget,
        package_asset: Asset,
        delivery_payload: dict,
    ) -> PublishAdapterResult:
        oauth_access = self._youtube_oauth_access(target)
        if oauth_access.access_token is None:
            return PublishAdapterResult(
                status="failed",
                remote_job_id=None,
                publish_url=None,
                metadata={
                    "adapter_protocol": "youtube_data_api_resumable_upload",
                    "error": "youtube_oauth_access_token_unavailable",
                    "oauth_access": oauth_access.metadata,
                    "package_uploaded": False,
                },
            )
        token = oauth_access.access_token
        package_path = self._package_path(package_asset)
        if package_path is None:
            return PublishAdapterResult(
                status="failed",
                remote_job_id=None,
                publish_url=None,
                metadata={
                    "adapter_protocol": "youtube_data_api_resumable_upload",
                    "error": "youtube_package_local_path_unavailable",
                    "package_uploaded": False,
                },
            )
        video_entry = self._youtube_package_video_entry(package_path)
        if video_entry is None:
            return PublishAdapterResult(
                status="failed",
                remote_job_id=None,
                publish_url=None,
                metadata={
                    "adapter_protocol": "youtube_data_api_resumable_upload",
                    "error": "youtube_package_missing_video_entry",
                    "package_uploaded": False,
                },
            )
        video_name, video_bytes = video_entry
        video_content_type = self._youtube_video_content_type(video_name)
        initiate_url = self._youtube_resumable_session_url(target)
        video_metadata = self._youtube_video_metadata(delivery_payload, target)
        initiate_headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json",
            "content-type": "application/json; charset=UTF-8",
            "x-upload-content-type": video_content_type,
            "x-upload-content-length": str(len(video_bytes)),
        }
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self._target_timeout(target),
            ) as client:
                initiate_response = client.post(
                    initiate_url,
                    headers=initiate_headers,
                    json=video_metadata,
                )
                session_uri = initiate_response.headers.get("location")
                initiate_payload = self._response_payload(initiate_response)
                if not initiate_response.is_success or not session_uri:
                    return PublishAdapterResult(
                        status="failed",
                        remote_job_id=None,
                        publish_url=None,
                        metadata={
                            "adapter_protocol": "youtube_data_api_resumable_upload",
                            "initiate_upload_endpoint": self._url_endpoint_evidence(
                                initiate_url
                            ),
                            "initiate_status_code": initiate_response.status_code,
                            "initiate_response": self._safe_response_payload(initiate_payload),
                            "session_uri_present": bool(session_uri),
                            "package_uploaded": False,
                        },
                    )
                upload_response = client.put(
                    session_uri,
                    headers={
                        "authorization": f"Bearer {token}",
                        "content-type": video_content_type,
                        "content-length": str(len(video_bytes)),
                    },
                    content=video_bytes,
                )
                upload_payload = self._response_payload(upload_response)
                video_id = self._first_string(upload_payload, ("id", "video_id"))
                thumbnail_result = self._upload_youtube_thumbnail(
                    client=client,
                    target=target,
                    token=token,
                    package_path=package_path,
                    video_id=video_id,
                )
                caption_results = self._upload_youtube_captions(
                    client=client,
                    target=target,
                    token=token,
                    package_path=package_path,
                    video_id=video_id,
                    default_language=str(
                        delivery_payload.get("language") or target.default_language
                    ),
                    default_name=str(delivery_payload.get("title") or "DialectiCore Episode"),
                )
        except (OSError, httpx.HTTPError, RuntimeError, zipfile.BadZipFile) as exc:
            return PublishAdapterResult(
                status="failed",
                remote_job_id=None,
                publish_url=None,
                metadata={
                    "adapter_protocol": "youtube_data_api_resumable_upload",
                    "initiate_upload_endpoint": self._url_endpoint_evidence(initiate_url),
                    "error": str(exc),
                    "package_uploaded": False,
                },
            )

        caption_status = self._youtube_caption_upload_status(caption_results)
        ancillary_failed = upload_response.is_success and (
            thumbnail_result.get("status") == "failed" or caption_status == "failed"
        )
        publish_url = (
            f"https://www.youtube.com/watch?v={video_id}"
            if upload_response.is_success and video_id
            else None
        )
        return PublishAdapterResult(
            status="completed" if upload_response.is_success and not ancillary_failed else "failed",
            remote_job_id=video_id,
            publish_url=publish_url,
            metadata={
                "adapter_protocol": "youtube_data_api_resumable_upload",
                "initiate_upload_endpoint": self._url_endpoint_evidence(initiate_url),
                "initiate_status_code": initiate_response.status_code,
                "session_uri_present": True,
                "oauth_access": oauth_access.metadata,
                "upload_status_code": upload_response.status_code,
                "upload_response": self._safe_response_payload(upload_payload),
                "video_entry_name": video_name,
                "video_content_type": video_content_type,
                "video_size_bytes": len(video_bytes),
                "package_uploaded": True,
                "thumbnail_upload": thumbnail_result.get("status"),
                "thumbnail_upload_result": thumbnail_result,
                "caption_upload": caption_status,
                "caption_upload_count": sum(
                    1 for result in caption_results if result.get("status") == "completed"
                ),
                "caption_upload_results": caption_results,
                "subtitle_upload": caption_status,
            },
        )

    def _publish_http(
        self,
        target: PublisherTarget,
        package_asset: Asset,
        delivery_payload: dict,
    ) -> PublishAdapterResult:
        if not target.base_url:
            raise ValueError(f"publisher target {target.id} has no base_url")
        headers = self._request_headers(target)
        url = self._delivery_url(target)
        package_path = self._package_path(package_asset)
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self._target_timeout(target),
                headers=headers,
            ) as client:
                if package_path is not None:
                    with package_path.open("rb") as package_file:
                        response = client.post(
                            url,
                            data={"payload": json.dumps(delivery_payload, sort_keys=True)},
                            files={
                                "package": (
                                    package_path.name,
                                    package_file,
                                    package_asset.mime_type or "application/zip",
                                )
                            },
                        )
                else:
                    response = client.post(url, json=delivery_payload)
        except (OSError, httpx.HTTPError, RuntimeError) as exc:
            return PublishAdapterResult(
                status="failed",
                remote_job_id=None,
                publish_url=None,
                metadata={
                    "request_endpoint": self._url_endpoint_evidence(url),
                    "error": str(exc),
                    "package_uploaded": False,
                },
            )

        payload = self._response_payload(response)
        remote_job_id = self._first_string(
            payload,
            ("remote_job_id", "job_id", "id", "video_id"),
        )
        publish_url = self._first_string(
            payload,
            ("publish_url", "url", "watch_url", "video_url"),
        )
        return PublishAdapterResult(
            status="completed" if response.is_success else "failed",
            remote_job_id=remote_job_id,
            publish_url=publish_url,
            metadata={
                "request_endpoint": self._url_endpoint_evidence(url),
                "status_code": response.status_code,
                "response": self._safe_response_payload(payload),
                "package_uploaded": package_path is not None,
            },
        )

    def _target(self, target_id: str, targets: list[PublisherTarget]) -> PublisherTarget:
        for target in targets:
            if target.id == target_id:
                return target
        raise ValueError(f"unknown publisher target {target_id}")

    def _package_asset(self, episode: Episode, package_asset_id: UUID | None) -> Asset:
        if package_asset_id is not None:
            package_asset = next(
                (
                    asset
                    for asset in episode.assets
                    if asset.id == package_asset_id and asset.asset_type == AssetType.export_package
                ),
                None,
            )
            if package_asset is None:
                raise ValueError("package asset not found")
            if package_asset.status != "completed":
                raise ValueError("package asset is not completed")
            return package_asset
        packages = [
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.export_package and asset.status == "completed"
        ]
        if not packages:
            raise ValueError("episode has no completed YouTube package")
        return packages[-1]

    def _ensure_package_qc_allows_publish(
        self,
        episode: Episode,
        package_asset: Asset,
        *,
        require_present: bool = False,
    ) -> None:
        package_qc = [
            result
            for result in episode.quality_results
            if result.check_type == "youtube_package_integrity"
            and result.target_id == str(package_asset.id)
        ]
        if not package_qc and require_present:
            raise ValueError("YouTube package QC is required before live publishing")
        if package_qc and (
            package_qc[-1].status == "fail" or package_qc[-1].severity == QualitySeverity.fail
        ):
            raise ValueError("failing YouTube package QC blocks publishing")

    def _production_manifest_asset(
        self,
        episode: Episode,
        package_asset: Asset,
    ) -> Asset | None:
        manifests = [
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.production_manifest
            and asset.status == "completed"
            and asset.source_entity_type == "export_package"
            and asset.source_entity_id == str(package_asset.id)
        ]
        return manifests[-1] if manifests else None

    def _production_manifest_asset_valid(
        self,
        manifest_asset: Asset,
        package_asset: Asset,
    ) -> bool:
        manifest = manifest_asset.generation_metadata.get("production_manifest")
        if not isinstance(manifest, dict):
            return False
        if manifest.get("schema_version") != "production_manifest.v1":
            return False
        delivery_package = manifest.get("delivery_package")
        if not isinstance(delivery_package, dict):
            return False
        embedded_package_id = delivery_package.get("asset_id")
        if not embedded_package_id or str(embedded_package_id) != str(package_asset.id):
            return False
        embedded_checksum = delivery_package.get("checksum")
        if (
            embedded_checksum
            and package_asset.checksum
            and str(embedded_checksum) != str(package_asset.checksum)
        ):
            return False
        embedded_storage_uri = delivery_package.get("storage_uri")
        if (
            embedded_storage_uri
            and package_asset.storage_uri
            and str(embedded_storage_uri) != str(package_asset.storage_uri)
        ):
            return False
        embedded_package_id = delivery_package.get("package_id")
        current_package_id = package_asset.generation_metadata.get("package_id")
        if (
            embedded_package_id
            and current_package_id
            and str(embedded_package_id) != str(current_package_id)
        ):
            return False
        return self._production_manifest_talkshow_visuals_valid(manifest)

    def _production_manifest_talkshow_visuals_valid(self, manifest: dict) -> bool:
        talkshow_visuals = manifest.get("talkshow_visuals")
        if not isinstance(talkshow_visuals, dict):
            return not self._production_manifest_has_reusable_visual_segments(manifest)
        if talkshow_visuals.get("schema_version") != "talkshow_visual_handoff.v1":
            return False
        for role in ("reaction_loop", "studio_scene"):
            section = talkshow_visuals.get(role)
            if not isinstance(section, dict):
                return False
            expected_count = int(section.get("expected_segment_count") or 0)
            linked_count = int(section.get("linked_segment_count") or 0)
            missing_ids = section.get("missing_segment_ids")
            if (
                expected_count > linked_count
                or (isinstance(missing_ids, list) and missing_ids)
                or section.get("ready") is False
            ):
                return False
        return talkshow_visuals.get("ready") is not False

    def _production_manifest_has_reusable_visual_segments(self, manifest: dict) -> bool:
        for segment in manifest.get("timeline_segments", []):
            if not isinstance(segment, dict):
                continue
            if segment.get("reaction_visual_asset_id") or segment.get("studio_scene_asset_id"):
                return True
            for layer in segment.get("visual_layers", []):
                if isinstance(layer, dict) and layer.get("role") in {
                    "reaction_loop",
                    "studio_scene",
                }:
                    return True
        return False

    def _latest_publish_job(
        self,
        episode: Episode,
        target_id: str,
        package_asset_id: UUID,
    ) -> PublishJob | None:
        jobs = [
            job
            for job in episode.publish_jobs
            if job.publisher_target_id == target_id and job.package_asset_id == package_asset_id
        ]
        return jobs[-1] if jobs else None

    def _delivery_payload(
        self,
        episode: Episode,
        package_asset: Asset,
        target: PublisherTarget,
        production_manifest_asset: Asset | None,
    ) -> dict:
        manifest = package_asset.generation_metadata.get("youtube_package_manifest", {})
        if not isinstance(manifest, dict):
            manifest = {}
        production_manifest = (
            production_manifest_asset.generation_metadata.get("production_manifest", {})
            if production_manifest_asset is not None
            else {}
        )
        if not isinstance(production_manifest, dict):
            production_manifest = {}
        tags = [
            *target.default_tags,
            *(tag for tag in manifest.get("tags", []) if isinstance(tag, str)),
        ]
        return {
            "schema_version": "publish_delivery_payload.v1",
            "episode_id": str(episode.id),
            "package_asset_id": str(package_asset.id),
            "package_uri": package_asset.storage_uri,
            "package_checksum": package_asset.checksum,
            "production_manifest_asset_id": (
                str(production_manifest_asset.id) if production_manifest_asset is not None else None
            ),
            "production_manifest_uri": (
                production_manifest_asset.storage_uri
                if production_manifest_asset is not None
                else None
            ),
            "production_manifest_checksum": (
                production_manifest_asset.checksum
                if production_manifest_asset is not None
                else None
            ),
            "production_manifest_schema_version": production_manifest.get("schema_version"),
            "platform": target.platform,
            "channel_id": target.channel_id,
            "privacy_status": target.privacy_status,
            "title": manifest.get("title") or episode.title,
            "description": manifest.get("description") or "",
            "tags": sorted(set(tags)),
            "language": manifest.get("language") or target.default_language,
            "video_uri": manifest.get("render_uri"),
            "thumbnail_uri": manifest.get("thumbnail_uri"),
            "subtitle_count": len(manifest.get("subtitles", [])),
            "chapter_count": len(manifest.get("chapters", [])),
            "evidence_lineage": manifest.get("evidence_lineage", {}),
        }

    def _publish_qc(
        self,
        episode: Episode,
        job: PublishJob,
        package_asset: Asset,
        target: PublisherTarget,
    ) -> QualityResult:
        issues: list[dict[str, str]] = []
        if not package_asset.storage_uri:
            issues.append({"severity": "fail", "issue": "publish_package_missing_storage"})
        if not package_asset.checksum:
            issues.append({"severity": "fail", "issue": "publish_package_missing_checksum"})
        if not job.delivery_payload.get("title"):
            issues.append({"severity": "fail", "issue": "publish_payload_missing_title"})
        if not job.delivery_payload.get("video_uri"):
            issues.append({"severity": "fail", "issue": "publish_payload_missing_video_uri"})
        live_delivery = not job.dry_run and target.adapter_type != "mock"
        payload_package_asset_id = job.delivery_payload.get("package_asset_id")
        payload_package_checksum = job.delivery_payload.get("package_checksum")
        package_checksum_matches = (
            bool(payload_package_checksum)
            and bool(package_asset.checksum)
            and payload_package_checksum == package_asset.checksum
        )
        if live_delivery and str(payload_package_asset_id or "") != str(package_asset.id):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "publish_payload_package_asset_id_mismatch",
                }
            )
        if live_delivery and not package_checksum_matches:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "publish_payload_package_checksum_mismatch",
                }
            )
        production_manifest_asset_id = job.delivery_payload.get("production_manifest_asset_id")
        production_manifest_checksum = job.delivery_payload.get("production_manifest_checksum")
        production_manifest_schema_version = job.delivery_payload.get(
            "production_manifest_schema_version"
        )
        if live_delivery and not production_manifest_asset_id:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "publish_payload_missing_production_manifest_asset_id",
                }
            )
        if live_delivery and not production_manifest_checksum:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "publish_payload_missing_production_manifest_checksum",
                }
            )
        if live_delivery and production_manifest_schema_version != "production_manifest.v1":
            issues.append(
                {
                    "severity": "fail",
                    "issue": "publish_payload_invalid_production_manifest_schema_version",
                }
            )
        if not target.enabled:
            issues.append({"severity": "fail", "issue": "publisher_target_disabled"})
        if job.dry_run:
            issues.append({"severity": "warning", "issue": "publish_job_is_dry_run"})
        if job.status == "failed":
            issues.append({"severity": "fail", "issue": "publish_delivery_failed"})
        failure_count = sum(1 for issue in issues if issue["severity"] == "fail")
        warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
        status = "fail" if failure_count else "warning" if warning_count else "pass"
        severity = (
            QualitySeverity.fail
            if failure_count
            else QualitySeverity.warning
            if warning_count
            else QualitySeverity.pass_
        )
        return QualityResult(
            episode_id=episode.id,
            target_type="publish_job",
            target_id=str(job.id),
            check_type="publish_delivery_integrity",
            severity=severity,
            status=status,
            details={
                "publish_job_id": str(job.id),
                "publisher_target_id": target.id,
                "platform": target.platform,
                "adapter_type": target.adapter_type,
                "job_status": job.status,
                "package_asset_id": str(package_asset.id),
                "payload_package_asset_id": payload_package_asset_id,
                "package_checksum_present": bool(package_asset.checksum),
                "payload_package_checksum_present": bool(payload_package_checksum),
                "package_checksum_matches": package_checksum_matches,
                "production_manifest_asset_id": production_manifest_asset_id,
                "production_manifest_checksum_present": bool(production_manifest_checksum),
                "production_manifest_schema_version": production_manifest_schema_version,
                "dry_run": job.dry_run,
                "publish_url": job.publish_url,
                "remote_job_id": job.remote_job_id,
                "resumable_upload": bool(
                    job.result_metadata.get("adapter_protocol")
                    == "youtube_data_api_resumable_upload"
                ),
                "session_uri_present": bool(job.result_metadata.get("session_uri_present")),
                "youtube_video_id": job.result_metadata.get("upload_response", {}).get("id")
                if isinstance(job.result_metadata.get("upload_response"), dict)
                else job.remote_job_id,
                "video_size_bytes": job.result_metadata.get("video_size_bytes"),
                "package_uploaded": job.result_metadata.get("package_uploaded"),
                "thumbnail_upload": job.result_metadata.get("thumbnail_upload"),
                "caption_upload": job.result_metadata.get("caption_upload"),
                "caption_upload_count": job.result_metadata.get("caption_upload_count"),
                "subtitle_upload": job.result_metadata.get("subtitle_upload"),
                "subtitle_count": int(job.delivery_payload.get("subtitle_count") or 0),
                "chapter_count": int(job.delivery_payload.get("chapter_count") or 0),
                "evidence_citation_count": len(
                    job.delivery_payload.get("evidence_lineage", {}).get(
                        "citation_links",
                        [],
                    )
                ),
                "issue_count": len(issues),
                "failure_count": failure_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _request_headers(self, target: PublisherTarget) -> dict[str, str]:
        headers = {"accept": "application/json"}
        token = self.secret_resolver.resolve(target.credential_reference)
        if token:
            headers["authorization"] = f"Bearer {token}"
        custom_headers = target.capabilities.get("headers", {})
        if isinstance(custom_headers, dict):
            for key, value in custom_headers.items():
                if isinstance(key, str) and isinstance(value, str):
                    headers[key] = value
        return headers

    def _target_timeout(self, target: PublisherTarget) -> httpx.Timeout:
        timeout_seconds = target.capabilities.get("timeout_seconds", 30)
        try:
            timeout = max(1.0, float(timeout_seconds))
        except (TypeError, ValueError):
            timeout = 30.0
        return httpx.Timeout(timeout)

    def _delivery_url(self, target: PublisherTarget) -> str:
        if not target.base_url:
            return ""
        delivery_path = str(target.capabilities.get("delivery_path") or "/publish")
        return f"{target.base_url.rstrip('/')}/{delivery_path.lstrip('/')}"

    def _url_endpoint_evidence(self, url: str) -> dict:
        parsed = urlparse(url)
        query_keys = sorted(
            {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
        )
        return {
            "configured": bool(url),
            "scheme": parsed.scheme or None,
            "path": parsed.path or None,
            "query_keys": query_keys,
        }

    def _credential_reference_evidence(self, label: str, reference: object) -> dict:
        value = reference if isinstance(reference, str) and reference else None
        return {
            f"{label}_reference_configured": value is not None,
            f"{label}_reference_scheme": credential_reference_scheme(value) if value else None,
        }

    def _youtube_oauth_access(self, target: PublisherTarget) -> YouTubeOAuthAccess:
        access_metadata: dict = {
            **self._credential_reference_evidence(
                "access_token",
                target.credential_reference,
            ),
            **self._credential_reference_evidence(
                "refresh_token",
                target.capabilities.get("oauth_refresh_token_reference"),
            ),
            **self._credential_reference_evidence(
                "client_id",
                target.capabilities.get("oauth_client_id_reference"),
            ),
            **self._credential_reference_evidence(
                "client_secret",
                target.capabilities.get("oauth_client_secret_reference"),
            ),
            "token_endpoint": self._url_endpoint_evidence(
                self._youtube_oauth_token_url(target)
            ),
        }
        if target.credential_reference:
            try:
                access_token = self.secret_resolver.resolve(target.credential_reference)
            except RuntimeError as exc:
                access_metadata["access_token_error"] = str(exc)
            else:
                if access_token:
                    return YouTubeOAuthAccess(
                        access_token=access_token,
                        metadata={
                            **access_metadata,
                            "source": "access_token_reference",
                            "refreshed": False,
                        },
                    )

        refreshed = self._refresh_youtube_oauth_access(target, access_metadata)
        if refreshed.access_token:
            return refreshed
        return YouTubeOAuthAccess(
            access_token=None,
            metadata={
                **access_metadata,
                **refreshed.metadata,
                "source": "unavailable",
                "refreshed": False,
                "error": refreshed.metadata.get(
                    "error",
                    "youtube_oauth_credential_reference_missing_or_unavailable",
                ),
            },
        )

    def _refresh_youtube_oauth_access(
        self,
        target: PublisherTarget,
        access_metadata: dict,
    ) -> YouTubeOAuthAccess:
        if not self._youtube_refresh_configured(target):
            return YouTubeOAuthAccess(
                access_token=None,
                metadata={"error": "youtube_oauth_refresh_references_not_configured"},
            )

        refresh_token_reference = str(
            target.capabilities.get("oauth_refresh_token_reference") or ""
        )
        client_id_reference = str(target.capabilities.get("oauth_client_id_reference") or "")
        client_secret_reference = str(
            target.capabilities.get("oauth_client_secret_reference") or ""
        )
        try:
            refresh_token = self.secret_resolver.resolve(refresh_token_reference)
            client_id = self.secret_resolver.resolve(client_id_reference)
            client_secret = self.secret_resolver.resolve(client_secret_reference)
        except RuntimeError as exc:
            return YouTubeOAuthAccess(
                access_token=None,
                metadata={"error": str(exc), "source": "refresh_token_reference"},
            )
        if not refresh_token or not client_id or not client_secret:
            return YouTubeOAuthAccess(
                access_token=None,
                metadata={
                    "error": "youtube_oauth_refresh_secret_reference_unavailable",
                    "source": "refresh_token_reference",
                },
            )

        token_url = self._youtube_oauth_token_url(target)
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self._target_timeout(target),
            ) as client:
                response = client.post(
                    token_url,
                    headers={"accept": "application/json"},
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            return YouTubeOAuthAccess(
                access_token=None,
                metadata={
                    **access_metadata,
                    "source": "refresh_token_reference",
                    "refreshed": False,
                    "refresh_status": "failed",
                    "refresh_error": str(exc),
                    "error": "youtube_oauth_refresh_request_failed",
                },
            )

        payload = self._response_payload(response)
        token_value = self._first_string(payload, ("access_token",))
        metadata = {
            **access_metadata,
            "source": "refresh_token_reference",
            "refreshed": response.is_success and bool(token_value),
            "refresh_status": "completed" if response.is_success else "failed",
            "refresh_status_code": response.status_code,
            "expires_in_seconds": payload.get("expires_in"),
            "scope": payload.get("scope"),
            "token_type": payload.get("token_type"),
        }
        if not response.is_success or not token_value:
            metadata["refresh_response"] = self._safe_oauth_response_payload(payload)
            metadata["error"] = "youtube_oauth_refresh_failed"
            return YouTubeOAuthAccess(access_token=None, metadata=metadata)
        return YouTubeOAuthAccess(access_token=token_value, metadata=metadata)

    def _youtube_refresh_configured(self, target: PublisherTarget) -> bool:
        return all(
            isinstance(target.capabilities.get(key), str) and bool(target.capabilities.get(key))
            for key in (
                "oauth_refresh_token_reference",
                "oauth_client_id_reference",
                "oauth_client_secret_reference",
            )
        )

    def _youtube_oauth_token_url(self, target: PublisherTarget) -> str:
        token_url = target.capabilities.get("oauth_token_url")
        if isinstance(token_url, str) and token_url:
            return token_url
        return "https://oauth2.googleapis.com/token"

    def _safe_oauth_response_payload(self, payload: dict) -> object:
        return self._safe_response_payload(payload)

    def _safe_response_payload(self, payload: object) -> object:
        return safe_provider_response_payload(payload)

    def _is_sensitive_response_key(self, key: str) -> bool:
        return is_sensitive_provider_response_key(key)

    def _youtube_oauth_token(self, target: PublisherTarget) -> str | None:
        try:
            return self._youtube_oauth_access(target).access_token
        except RuntimeError:
            return None

    def _youtube_health_url(self, target: PublisherTarget) -> str:
        health_path = str(
            target.capabilities.get("health_path") or "/youtube/v3/channels?part=id&mine=true"
        )
        return f"{self._youtube_api_base_url(target).rstrip('/')}/{health_path.lstrip('/')}"

    def _youtube_resumable_session_url(self, target: PublisherTarget) -> str:
        upload_path = str(
            target.capabilities.get("upload_path")
            or "/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
        )
        return f"{self._youtube_upload_base_url(target).rstrip('/')}/{upload_path.lstrip('/')}"

    def _youtube_api_base_url(self, target: PublisherTarget) -> str:
        base_url = target.capabilities.get("api_base_url") or target.base_url
        return str(base_url or YOUTUBE_API_BASE_URL)

    def _youtube_upload_base_url(self, target: PublisherTarget) -> str:
        base_url = target.capabilities.get("upload_base_url") or target.base_url
        return str(base_url or YOUTUBE_UPLOAD_BASE_URL)

    def _youtube_thumbnail_upload_url(
        self,
        target: PublisherTarget,
        video_id: str,
    ) -> str:
        upload_path = str(
            target.capabilities.get("thumbnail_upload_path") or "/upload/youtube/v3/thumbnails/set"
        )
        url = f"{self._youtube_upload_base_url(target).rstrip('/')}/{upload_path.lstrip('/')}"
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode({'videoId': video_id})}"

    def _youtube_caption_upload_url(self, target: PublisherTarget) -> str:
        upload_path = str(
            target.capabilities.get("caption_upload_path")
            or "/upload/youtube/v3/captions?part=snippet"
        )
        return f"{self._youtube_upload_base_url(target).rstrip('/')}/{upload_path.lstrip('/')}"

    def _upload_youtube_thumbnail(
        self,
        client: httpx.Client,
        target: PublisherTarget,
        token: str,
        package_path: Path,
        video_id: str | None,
    ) -> dict:
        if not bool(target.capabilities.get("thumbnail_upload", True)):
            return {"status": "skipped", "reason": "thumbnail_upload_disabled"}
        if not video_id:
            return {"status": "skipped", "reason": "youtube_video_id_unavailable"}
        thumbnail_entry = self._youtube_package_thumbnail_entry(package_path)
        if thumbnail_entry is None:
            return {"status": "skipped", "reason": "youtube_package_missing_thumbnail_entry"}
        entry_name, thumbnail_bytes = thumbnail_entry
        content_type = self._youtube_thumbnail_content_type(entry_name)
        url = self._youtube_thumbnail_upload_url(target, video_id)
        try:
            response = client.post(
                url,
                headers={
                    "authorization": f"Bearer {token}",
                    "accept": "application/json",
                    "content-type": content_type,
                    "content-length": str(len(thumbnail_bytes)),
                },
                content=thumbnail_bytes,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            return {
                "status": "failed",
                "upload_endpoint": self._url_endpoint_evidence(url),
                "entry_name": entry_name,
                "error": str(exc),
            }
        return {
            "status": "completed" if response.is_success else "failed",
            "upload_endpoint": self._url_endpoint_evidence(url),
            "entry_name": entry_name,
            "content_type": content_type,
            "size_bytes": len(thumbnail_bytes),
            "status_code": response.status_code,
            "response": self._safe_response_payload(self._response_payload(response)),
        }

    def _upload_youtube_captions(
        self,
        client: httpx.Client,
        target: PublisherTarget,
        token: str,
        package_path: Path,
        video_id: str | None,
        default_language: str,
        default_name: str,
    ) -> list[dict]:
        if not bool(
            target.capabilities.get(
                "caption_upload",
                target.capabilities.get("subtitle_upload", True),
            )
        ):
            return [{"status": "skipped", "reason": "caption_upload_disabled"}]
        if not video_id:
            return [{"status": "skipped", "reason": "youtube_video_id_unavailable"}]
        caption_entries = self._youtube_package_caption_entries(
            package_path,
            default_language=default_language,
            default_name=default_name,
            is_draft=bool(target.capabilities.get("caption_is_draft", False)),
        )
        if not caption_entries:
            return [{"status": "skipped", "reason": "youtube_package_missing_caption_entries"}]

        url = self._youtube_caption_upload_url(target)
        results = []
        for entry in caption_entries:
            metadata = {
                "snippet": {
                    "videoId": video_id,
                    "language": entry["language"],
                    "name": entry["name"],
                    "isDraft": entry["is_draft"],
                }
            }
            body, content_type = self._multipart_related_body(
                [
                    (
                        "application/json; charset=UTF-8",
                        json.dumps(metadata, sort_keys=True).encode("utf-8"),
                    ),
                    (entry["content_type"], entry["payload"]),
                ]
            )
            try:
                response = client.post(
                    url,
                    headers={
                        "authorization": f"Bearer {token}",
                        "accept": "application/json",
                        "content-type": content_type,
                        "content-length": str(len(body)),
                    },
                    content=body,
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                results.append(
                    {
                        "status": "failed",
                        "upload_endpoint": self._url_endpoint_evidence(url),
                        "entry_name": entry["entry_name"],
                        "language": entry["language"],
                        "name": entry["name"],
                        "error": str(exc),
                    }
                )
                continue
            results.append(
                {
                    "status": "completed" if response.is_success else "failed",
                    "upload_endpoint": self._url_endpoint_evidence(url),
                    "entry_name": entry["entry_name"],
                    "language": entry["language"],
                    "name": entry["name"],
                    "content_type": entry["content_type"],
                    "size_bytes": len(entry["payload"]),
                    "status_code": response.status_code,
                    "response": self._safe_response_payload(self._response_payload(response)),
                }
            )
        return results

    def _youtube_caption_upload_status(self, results: list[dict]) -> str:
        if not results:
            return "skipped"
        statuses = {str(result.get("status")) for result in results}
        if "failed" in statuses:
            return "failed"
        if statuses == {"skipped"}:
            return "skipped"
        if statuses <= {"completed", "skipped"} and "completed" in statuses:
            return "completed"
        return "failed"

    def _youtube_video_metadata(
        self,
        delivery_payload: dict,
        target: PublisherTarget,
    ) -> dict:
        snippet = {
            "title": str(delivery_payload.get("title") or "DialectiCore Episode"),
            "description": str(delivery_payload.get("description") or ""),
            "tags": [
                tag for tag in delivery_payload.get("tags", []) if isinstance(tag, str) and tag
            ],
            "defaultLanguage": str(delivery_payload.get("language") or target.default_language),
        }
        category_id = target.capabilities.get("youtube_category_id")
        if isinstance(category_id, str) and category_id:
            snippet["categoryId"] = category_id
        return {
            "snippet": snippet,
            "status": {
                "privacyStatus": target.privacy_status,
                "selfDeclaredMadeForKids": bool(
                    target.capabilities.get("self_declared_made_for_kids", False)
                ),
            },
        }

    def _youtube_package_entry_names(self, package_path: Path | None) -> list[str]:
        if package_path is None:
            return []
        try:
            with zipfile.ZipFile(package_path) as archive:
                return [
                    name
                    for name in archive.namelist()
                    if not name.endswith("/") and not name.startswith("__MACOSX/")
                ]
        except (OSError, zipfile.BadZipFile):
            return []

    def _youtube_package_video_entry(
        self,
        package_path: Path,
    ) -> tuple[str, bytes] | None:
        with zipfile.ZipFile(package_path) as archive:
            video_names = [
                name
                for name in archive.namelist()
                if name.startswith("video/") and not name.endswith("/")
            ]
            if not video_names:
                return None
            video_name = sorted(video_names)[0]
            return video_name, archive.read(video_name)

    def _youtube_package_thumbnail_entry(
        self,
        package_path: Path,
    ) -> tuple[str, bytes] | None:
        with zipfile.ZipFile(package_path) as archive:
            thumbnail_names = [
                name
                for name in archive.namelist()
                if name.startswith("thumbnail/") and not name.endswith("/")
            ]
            if not thumbnail_names:
                return None
            thumbnail_name = sorted(thumbnail_names)[0]
            return thumbnail_name, archive.read(thumbnail_name)

    def _youtube_package_caption_entries(
        self,
        package_path: Path,
        default_language: str,
        default_name: str,
        is_draft: bool,
    ) -> list[dict]:
        with zipfile.ZipFile(package_path) as archive:
            manifest = self._youtube_package_manifest_from_archive(archive)
            manifest_subtitles = {
                subtitle.get("path"): subtitle
                for subtitle in manifest.get("subtitles", [])
                if isinstance(subtitle, dict) and isinstance(subtitle.get("path"), str)
            }
            caption_names = [
                name
                for name in archive.namelist()
                if name.startswith("subtitles/") and not name.endswith("/")
            ]
            entries = []
            for caption_name in sorted(caption_names):
                manifest_entry = manifest_subtitles.get(caption_name, {})
                language = (
                    manifest_entry.get("language")
                    if isinstance(manifest_entry.get("language"), str)
                    else None
                )
                language = language or Path(caption_name).stem or default_language
                track_name = (
                    manifest_entry.get("name")
                    if isinstance(manifest_entry.get("name"), str)
                    else None
                )
                track_name = track_name or f"{default_name} {language}"
                entries.append(
                    {
                        "entry_name": caption_name,
                        "language": language,
                        "name": track_name[:150],
                        "is_draft": bool(manifest_entry.get("is_draft", is_draft)),
                        "content_type": self._youtube_caption_content_type(caption_name),
                        "payload": archive.read(caption_name),
                    }
                )
            return entries

    def _youtube_package_manifest_from_archive(self, archive: zipfile.ZipFile) -> dict:
        try:
            payload = json.loads(archive.read("youtube-package.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _youtube_video_content_type(self, video_name: str) -> str:
        suffix = Path(video_name).suffix.lower()
        if suffix == ".mp4":
            return "video/mp4"
        if suffix == ".mov":
            return "video/quicktime"
        if suffix == ".webm":
            return "video/webm"
        return "application/octet-stream"

    def _youtube_thumbnail_content_type(self, thumbnail_name: str) -> str:
        suffix = Path(thumbnail_name).suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".png":
            return "image/png"
        return "application/octet-stream"

    def _youtube_caption_content_type(self, caption_name: str) -> str:
        suffix = Path(caption_name).suffix.lower()
        if suffix == ".vtt":
            return "text/vtt"
        if suffix == ".srt":
            return "application/x-subrip"
        if suffix in {".xml", ".ttml", ".dfxp"}:
            return "text/xml"
        return "application/octet-stream"

    def _multipart_related_body(
        self,
        parts: list[tuple[str, bytes]],
    ) -> tuple[bytes, str]:
        boundary = f"dialecticore-{uuid4().hex}"
        body = b""
        for content_type, payload in parts:
            body += f"--{boundary}\r\n".encode("ascii")
            body += f"Content-Type: {content_type}\r\n\r\n".encode("ascii")
            body += payload
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode("ascii")
        return body, f"multipart/related; boundary={boundary}"

    def _package_path(self, package_asset: Asset) -> Path | None:
        path_value = package_asset.generation_metadata.get("object_storage_path")
        if not isinstance(path_value, str) or not path_value:
            return None
        path = Path(path_value)
        if path.exists() and path.is_file():
            return path
        return None

    def _response_payload(self, response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:2000]}
        return payload if isinstance(payload, dict) else {"response": payload}

    def _first_string(self, payload: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None
