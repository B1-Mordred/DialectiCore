import json
import zipfile
from pathlib import Path

import httpx
import pytest
from app.domain.defaults import default_publisher_targets
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity
from app.domain.schemas import (
    Asset,
    EpisodeCreateRequest,
    PublisherTarget,
    PublishJob,
    PublishRequest,
    QualityResult,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.publisher_service import PublisherService
from tests.test_discussion_engine import definition


def test_publisher_service_records_mock_publish_job_and_qc() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.append(package_asset)

    published = PublisherService().publish_package(
        episode,
        PublishRequest(user_id="tester"),
        targets=default_publisher_targets(),
    )

    job = published.publish_jobs[-1]
    publish_qc = [
        result
        for result in published.quality_results
        if result.check_type == "publish_delivery_integrity"
    ][-1]
    assert job.status == "completed"
    assert job.dry_run is True
    assert job.publisher_target_id == "mock-youtube"
    assert job.delivery_payload["title"] == "Packaged Episode"
    assert job.delivery_payload["subtitle_count"] == 1
    assert job.publish_url and job.publish_url.startswith("mock://youtube/")
    assert publish_qc.status == "warning"
    assert publish_qc.details["warning_count"] == 1
    assert published.audit_events[-3].event_type == "publisher.job.submitted"
    assert published.audit_events[-2].event_type == "publisher.job.completed"
    assert published.audit_events[-1].event_type == "publisher.job.qc.completed"


def test_default_publisher_targets_include_disabled_youtube_resumable_template() -> None:
    targets = {target.id: target for target in default_publisher_targets()}

    assert targets["youtube-resumable"].adapter_type == "youtube_resumable"
    assert targets["youtube-resumable"].enabled is False
    assert targets["youtube-resumable"].credential_reference == ("env:YOUTUBE_OAUTH_ACCESS_TOKEN")
    assert targets["youtube-resumable"].capabilities["resumable_upload"] is True
    assert targets["youtube-resumable"].capabilities["thumbnail_upload"] is True
    assert targets["youtube-resumable"].capabilities["caption_upload"] is True
    assert targets["youtube-resumable"].capabilities["subtitle_upload"] is True
    assert targets["youtube-resumable"].capabilities["oauth_refresh_token_reference"] == (
        "env:YOUTUBE_OAUTH_REFRESH_TOKEN"
    )
    assert targets["youtube-resumable"].capabilities["oauth_client_id_reference"] == (
        "env:YOUTUBE_OAUTH_CLIENT_ID"
    )
    assert targets["youtube-resumable"].capabilities["oauth_client_secret_reference"] == (
        "env:YOUTUBE_OAUTH_CLIENT_SECRET"
    )
    assert targets["youtube-resumable"].capabilities["oauth_required"] is True


def test_publisher_service_checks_youtube_resumable_health(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_TEST_TOKEN", "youtube-token")
    target = PublisherTarget(
        id="youtube-live",
        name="YouTube Live",
        platform="youtube",
        adapter_type="youtube_resumable",
        base_url="https://youtube.test",
        credential_reference="env:YOUTUBE_TEST_TOKEN",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == ("https://youtube.test/youtube/v3/channels?part=id&mine=true")
        assert request.headers["authorization"] == "Bearer youtube-token"
        return httpx.Response(
            200,
            json={
                "capabilities": {
                    "channel_verified": True,
                    "accessToken": "leaked-youtube-health-token",
                    "nested": {"clientSecret": "leaked-youtube-health-secret"},
                }
            },
        )

    checked = PublisherService(transport=httpx.MockTransport(handler)).check_target_health(target)

    assert checked.health_status == "healthy"
    assert checked.capabilities["resumable_upload"] is True
    assert checked.capabilities["video_upload"] is True
    assert checked.capabilities["thumbnail_upload"] is True
    assert checked.capabilities["caption_upload"] is True
    assert checked.capabilities["subtitle_upload"] is True
    assert checked.capabilities["channel_verified"] is True
    assert checked.capabilities["health_endpoint"] == {
        "configured": True,
        "scheme": "https",
        "path": "/youtube/v3/channels",
        "query_keys": ["mine", "part"],
    }
    assert checked.capabilities["accessToken"] == "[redacted]"
    assert checked.capabilities["nested"]["clientSecret"] == "[redacted]"
    capabilities_json = json.dumps(checked.capabilities, sort_keys=True)
    assert "youtube.test" not in capabilities_json
    assert "leaked-youtube-health-token" not in capabilities_json
    assert "leaked-youtube-health-secret" not in capabilities_json


def test_publisher_service_refreshes_youtube_oauth_for_health(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "client-secret")
    target = PublisherTarget(
        id="youtube-live",
        name="YouTube Live",
        platform="youtube",
        adapter_type="youtube_resumable",
        base_url="https://youtube.test",
        capabilities={
            "oauth_refresh_token_reference": "env:YOUTUBE_REFRESH_TOKEN",
            "oauth_client_id_reference": "env:YOUTUBE_CLIENT_ID",
            "oauth_client_secret_reference": "env:YOUTUBE_CLIENT_SECRET",
            "oauth_token_url": "https://oauth.test/token",
        },
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "POST":
            assert str(request.url) == "https://oauth.test/token"
            body = request.read()
            assert b"grant_type=refresh_token" in body
            assert b"refresh_token=refresh-token" in body
            assert b"client_id=client-id" in body
            assert b"client_secret=client-secret" in body
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/youtube.upload",
                },
            )
        if request.method == "GET":
            assert request.headers["authorization"] == "Bearer refreshed-token"
            return httpx.Response(200, json={"capabilities": {"channel_verified": True}})
        return httpx.Response(404)

    checked = PublisherService(transport=httpx.MockTransport(handler)).check_target_health(target)

    assert requests == [
        ("POST", "https://oauth.test/token"),
        ("GET", "https://youtube.test/youtube/v3/channels?part=id&mine=true"),
    ]
    assert checked.health_status == "healthy"
    assert checked.capabilities["oauth_access"]["source"] == "refresh_token_reference"
    assert checked.capabilities["oauth_access"]["refreshed"] is True
    assert checked.capabilities["oauth_access"]["refresh_status_code"] == 200
    assert "access_token" not in checked.capabilities["oauth_access"]
    oauth_access_json = json.dumps(checked.capabilities["oauth_access"], sort_keys=True)
    assert "YOUTUBE_REFRESH_TOKEN" not in oauth_access_json
    assert "YOUTUBE_CLIENT_SECRET" not in oauth_access_json
    assert "oauth.test" not in oauth_access_json


def test_publisher_service_records_youtube_resumable_dry_run(tmp_path) -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_path = _youtube_package_path(tmp_path)
    package_asset = _package_asset(episode.id, object_storage_path=package_path)
    episode.assets.append(package_asset)
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    episode.assets.append(_production_manifest_asset(episode.id, package_asset))
    target = PublisherTarget(
        id="youtube-live",
        name="YouTube Live",
        platform="youtube",
        adapter_type="youtube_resumable",
        base_url="https://youtube.test",
        credential_reference="env:YOUTUBE_TEST_TOKEN",
    )

    published = PublisherService().publish_package(
        episode,
        PublishRequest(
            publisher_target_id=target.id,
            package_asset_id=package_asset.id,
            dry_run=True,
            user_id="tester",
        ),
        targets=[target],
    )

    job = published.publish_jobs[-1]
    publish_qc = [
        result
        for result in published.quality_results
        if result.check_type == "publish_delivery_integrity"
    ][-1]
    assert job.status == "completed"
    assert job.dry_run is True
    assert job.result_metadata["adapter_protocol"] == ("youtube_data_api_resumable_upload")
    assert job.result_metadata["initiate_upload_endpoint"] == {
        "configured": True,
        "scheme": "https",
        "path": "/upload/youtube/v3/videos",
        "query_keys": ["part", "uploadType"],
    }
    assert job.result_metadata["thumbnail_upload_endpoint"]["path"] == (
        "/upload/youtube/v3/thumbnails/set"
    )
    assert job.result_metadata["thumbnail_upload_endpoint"]["query_keys"] == ["videoId"]
    assert job.result_metadata["caption_upload_endpoint"]["path"] == (
        "/upload/youtube/v3/captions"
    )
    assert job.result_metadata["would_upload_video"] is True
    assert job.result_metadata["would_upload_thumbnail"] is True
    assert job.result_metadata["would_upload_subtitles"] is True
    assert "youtube.test" not in json.dumps(job.result_metadata, sort_keys=True)
    assert publish_qc.status == "warning"
    assert publish_qc.details["resumable_upload"] is True


def test_publisher_service_uploads_youtube_resumable_video(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("YOUTUBE_TEST_TOKEN", "youtube-token")
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_path = _youtube_package_path(tmp_path)
    package_asset = _package_asset(episode.id, object_storage_path=package_path)
    episode.assets.append(package_asset)
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    episode.assets.append(_production_manifest_asset(episode.id, package_asset))
    target = PublisherTarget(
        id="youtube-live",
        name="YouTube Live",
        platform="youtube",
        adapter_type="youtube_resumable",
        base_url="https://youtube.test",
        credential_reference="env:YOUTUBE_TEST_TOKEN",
        privacy_status="private",
        capabilities={"youtube_category_id": "28"},
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if (
            request.method == "POST"
            and str(request.url)
            == "https://youtube.test/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status"
        ):
            assert str(request.url) == (
                "https://youtube.test/upload/youtube/v3/videos"
                "?uploadType=resumable&part=snippet,status"
            )
            assert request.headers["authorization"] == "Bearer youtube-token"
            assert request.headers["x-upload-content-type"] == "video/mp4"
            assert request.headers["x-upload-content-length"] == str(len(b"video-bytes"))
            payload = json.loads(request.read().decode("utf-8"))
            assert payload["snippet"]["title"] == "Packaged Episode"
            assert payload["status"]["privacyStatus"] == "private"
            assert payload["snippet"]["categoryId"] == "28"
            return httpx.Response(
                200,
                headers={"location": "https://upload.youtube.test/session/upload-123"},
            )
        if request.method == "PUT":
            assert str(request.url) == "https://upload.youtube.test/session/upload-123"
            assert request.headers["authorization"] == "Bearer youtube-token"
            assert request.headers["content-type"] == "video/mp4"
            assert request.read() == b"video-bytes"
            return httpx.Response(
                201,
                json={
                    "id": "yt-video-123",
                    "access_token": "leaked-upload-token",
                    "nested": {"client_secret": "leaked-upload-secret"},
                },
            )
        if (
            request.method == "POST"
            and str(request.url)
            == "https://youtube.test/upload/youtube/v3/thumbnails/set?videoId=yt-video-123"
        ):
            assert request.headers["authorization"] == "Bearer youtube-token"
            assert request.headers["content-type"] == "image/jpeg"
            assert request.read() == b"thumbnail-bytes"
            return httpx.Response(
                200,
                json={
                    "kind": "youtube#thumbnailSetResponse",
                    "items": [],
                    "refresh_token": "leaked-thumbnail-token",
                },
            )
        if (
            request.method == "POST"
            and str(request.url) == "https://youtube.test/upload/youtube/v3/captions?part=snippet"
        ):
            assert request.headers["authorization"] == "Bearer youtube-token"
            assert request.headers["content-type"].startswith("multipart/related")
            body = request.read()
            assert b'"language": "en"' in body
            assert b'"name": "Packaged Episode en"' in body
            assert b'"videoId": "yt-video-123"' in body
            assert b"WEBVTT" in body
            return httpx.Response(
                200,
                json={
                    "id": "caption-en",
                    "snippet": {"language": "en", "name": "Packaged Episode en"},
                    "authorization": "Bearer leaked-caption-token",
                },
            )
        return httpx.Response(404)

    published = PublisherService(transport=httpx.MockTransport(handler)).publish_package(
        episode,
        PublishRequest(
            publisher_target_id=target.id,
            package_asset_id=package_asset.id,
            dry_run=False,
            user_id="tester",
        ),
        targets=[target],
    )

    job = published.publish_jobs[-1]
    publish_qc = [
        result
        for result in published.quality_results
        if result.check_type == "publish_delivery_integrity"
    ][-1]
    assert requests == [
        (
            "POST",
            "https://youtube.test/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
        ),
        ("PUT", "https://upload.youtube.test/session/upload-123"),
        (
            "POST",
            "https://youtube.test/upload/youtube/v3/thumbnails/set?videoId=yt-video-123",
        ),
        (
            "POST",
            "https://youtube.test/upload/youtube/v3/captions?part=snippet",
        ),
    ]
    assert job.status == "completed"
    assert job.dry_run is False
    assert job.remote_job_id == "yt-video-123"
    assert job.publish_url == "https://www.youtube.com/watch?v=yt-video-123"
    assert job.result_metadata["session_uri_present"] is True
    assert "resumable_session_uri" not in job.result_metadata
    assert job.result_metadata["initiate_upload_endpoint"]["path"] == (
        "/upload/youtube/v3/videos"
    )
    assert job.result_metadata["video_size_bytes"] == len(b"video-bytes")
    assert job.result_metadata["upload_response"]["access_token"] == "[redacted]"
    assert job.result_metadata["upload_response"]["nested"]["client_secret"] == ("[redacted]")
    assert job.result_metadata["thumbnail_upload"] == "completed"
    assert job.result_metadata["thumbnail_upload_result"]["status_code"] == 200
    assert (
        job.result_metadata["thumbnail_upload_result"]["response"]["refresh_token"] == "[redacted]"
    )
    assert job.result_metadata["caption_upload"] == "completed"
    assert job.result_metadata["subtitle_upload"] == "completed"
    assert job.result_metadata["caption_upload_count"] == 1
    assert job.result_metadata["caption_upload_results"][0]["language"] == "en"
    assert (
        job.result_metadata["caption_upload_results"][0]["response"]["authorization"]
        == "[redacted]"
    )
    metadata_json = json.dumps(job.result_metadata, sort_keys=True)
    assert "youtube.test" not in metadata_json
    assert "https://upload.youtube.test/session/upload-123" not in metadata_json
    assert "leaked-upload-token" not in metadata_json
    assert "leaked-upload-secret" not in metadata_json
    assert "leaked-thumbnail-token" not in metadata_json
    assert "leaked-caption-token" not in metadata_json
    assert publish_qc.status == "pass"
    assert publish_qc.details["adapter_type"] == "youtube_resumable"
    assert publish_qc.details["resumable_upload"] is True
    assert publish_qc.details["youtube_video_id"] == "yt-video-123"
    assert publish_qc.details["session_uri_present"] is True
    assert publish_qc.details["video_size_bytes"] == len(b"video-bytes")
    assert publish_qc.details["thumbnail_upload"] == "completed"
    assert publish_qc.details["caption_upload"] == "completed"
    assert publish_qc.details["caption_upload_count"] == 1
    assert job.delivery_payload["production_manifest_schema_version"] == ("production_manifest.v1")
    assert published.status == EpisodeStatus.completed


def test_publisher_service_refreshes_youtube_oauth_for_live_upload(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "client-secret")
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_path = _youtube_package_path(tmp_path)
    package_asset = _package_asset(episode.id, object_storage_path=package_path)
    episode.assets.append(package_asset)
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    episode.assets.append(_production_manifest_asset(episode.id, package_asset))
    target = PublisherTarget(
        id="youtube-live",
        name="YouTube Live",
        platform="youtube",
        adapter_type="youtube_resumable",
        base_url="https://youtube.test",
        privacy_status="private",
        capabilities={
            "oauth_refresh_token_reference": "env:YOUTUBE_REFRESH_TOKEN",
            "oauth_client_id_reference": "env:YOUTUBE_CLIENT_ID",
            "oauth_client_secret_reference": "env:YOUTUBE_CLIENT_SECRET",
            "oauth_token_url": "https://oauth.test/token",
            "thumbnail_upload": False,
            "caption_upload": False,
        },
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "POST" and str(request.url) == "https://oauth.test/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if (
            request.method == "POST"
            and str(request.url)
            == "https://youtube.test/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status"
        ):
            assert request.headers["authorization"] == "Bearer refreshed-token"
            return httpx.Response(
                200,
                headers={"location": "https://upload.youtube.test/session/upload-456"},
            )
        if request.method == "PUT":
            assert request.headers["authorization"] == "Bearer refreshed-token"
            return httpx.Response(201, json={"id": "yt-video-456"})
        return httpx.Response(404)

    published = PublisherService(transport=httpx.MockTransport(handler)).publish_package(
        episode,
        PublishRequest(
            publisher_target_id=target.id,
            package_asset_id=package_asset.id,
            dry_run=False,
            user_id="tester",
        ),
        targets=[target],
    )

    job = published.publish_jobs[-1]
    assert requests == [
        ("POST", "https://oauth.test/token"),
        (
            "POST",
            "https://youtube.test/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
        ),
        ("PUT", "https://upload.youtube.test/session/upload-456"),
    ]
    assert job.status == "completed"
    assert job.remote_job_id == "yt-video-456"
    assert job.result_metadata["oauth_access"]["source"] == "refresh_token_reference"
    assert job.result_metadata["oauth_access"]["refreshed"] is True
    assert "access_token" not in job.result_metadata["oauth_access"]
    oauth_access_json = json.dumps(job.result_metadata["oauth_access"], sort_keys=True)
    assert "YOUTUBE_REFRESH_TOKEN" not in oauth_access_json
    assert "YOUTUBE_CLIENT_SECRET" not in oauth_access_json
    assert "oauth.test" not in oauth_access_json
    assert job.result_metadata["thumbnail_upload"] == "skipped"
    assert job.result_metadata["caption_upload"] == "skipped"
    assert published.status == EpisodeStatus.completed


def test_publisher_service_delivers_package_to_http_target_and_qc(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PUBLISHER_TEST_TOKEN", "secret-token")
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(b"package-bytes")
    package_asset = _package_asset(episode.id, object_storage_path=package_path)
    episode.assets.append(package_asset)
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    episode.assets.append(_production_manifest_asset(episode.id, package_asset))
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        credential_reference="env:PUBLISHER_TEST_TOKEN",
        channel_id="channel-a",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://publisher.test/deliveries"
        assert request.headers["authorization"] == "Bearer secret-token"
        body = request.read()
        assert b"publish_delivery_payload.v1" in body
        assert b"production_manifest.v1" in body
        assert b"package-bytes" in body
        assert b"package.zip" in body
        return httpx.Response(
            202,
            json={
                "job_id": "upload-123",
                "publish_url": "https://publisher.test/watch/upload-123",
                "api_key": "leaked-api-key",
                "apiKey": "leaked-camel-api-key",
                "nested": {
                    "password": "leaked-password",
                    "clientSecret": "leaked-camel-secret",
                    "items": [{"secret": "leaked-secret"}],
                },
            },
        )

    published = PublisherService(transport=httpx.MockTransport(handler)).publish_package(
        episode,
        PublishRequest(
            publisher_target_id=target.id,
            package_asset_id=package_asset.id,
            dry_run=False,
            user_id="tester",
        ),
        targets=[target],
    )

    job = published.publish_jobs[-1]
    publish_qc = [
        result
        for result in published.quality_results
        if result.check_type == "publish_delivery_integrity"
    ][-1]
    assert job.status == "completed"
    assert job.dry_run is False
    assert job.remote_job_id == "upload-123"
    assert job.publish_url == "https://publisher.test/watch/upload-123"
    assert job.result_metadata["request_endpoint"] == {
        "configured": True,
        "scheme": "https",
        "path": "/deliveries",
        "query_keys": [],
    }
    assert job.result_metadata["status_code"] == 202
    assert job.result_metadata["response"]["api_key"] == "[redacted]"
    assert job.result_metadata["response"]["apiKey"] == "[redacted]"
    assert job.result_metadata["response"]["nested"]["password"] == "[redacted]"
    assert job.result_metadata["response"]["nested"]["clientSecret"] == "[redacted]"
    assert job.result_metadata["response"]["nested"]["items"][0]["secret"] == ("[redacted]")
    metadata_json = json.dumps(job.result_metadata, sort_keys=True)
    assert "leaked-api-key" not in metadata_json
    assert "leaked-camel-api-key" not in metadata_json
    assert "leaked-camel-secret" not in metadata_json
    assert "leaked-password" not in metadata_json
    assert "leaked-secret" not in metadata_json
    assert job.result_metadata["package_uploaded"] is True
    assert job.delivery_payload["production_manifest_asset_id"]
    assert job.delivery_payload["production_manifest_checksum"] == "sha256:manifest"
    assert publish_qc.status == "pass"
    assert publish_qc.severity == QualitySeverity.pass_
    assert publish_qc.details["adapter_type"] == "http"
    assert publish_qc.details["payload_package_asset_id"] == str(package_asset.id)
    assert publish_qc.details["payload_package_checksum_present"] is True
    assert publish_qc.details["package_checksum_matches"] is True
    assert publish_qc.details["production_manifest_asset_id"] == (
        job.delivery_payload["production_manifest_asset_id"]
    )
    assert publish_qc.details["production_manifest_checksum_present"] is True
    assert publish_qc.details["production_manifest_schema_version"] == "production_manifest.v1"
    assert publish_qc.details["failure_count"] == 0
    assert publish_qc.details["warning_count"] == 0
    assert published.status == EpisodeStatus.completed
    assert published.audit_events[-2].event_type == "publisher.job.completed"


def test_publisher_service_records_http_delivery_failure_with_qc() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.append(package_asset)
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    episode.assets.append(_production_manifest_asset(episode.id, package_asset))
    target = PublisherTarget(
        id="broken-generic",
        name="Broken Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    published = PublisherService(transport=httpx.MockTransport(handler)).publish_package(
        episode,
        PublishRequest(
            publisher_target_id=target.id,
            package_asset_id=package_asset.id,
            dry_run=False,
            user_id="tester",
        ),
        targets=[target],
    )

    job = published.publish_jobs[-1]
    publish_qc = [
        result
        for result in published.quality_results
        if result.check_type == "publish_delivery_integrity"
    ][-1]
    assert job.status == "failed"
    assert job.dry_run is False
    assert job.result_metadata["status_code"] == 503
    assert publish_qc.status == "fail"
    assert publish_qc.details["failure_count"] == 1
    assert publish_qc.details["issues"][-1]["issue"] == "publish_delivery_failed"
    assert published.audit_events[-2].event_type == "publisher.job.failed"


def test_publish_delivery_qc_fails_live_payload_without_manifest_handoff() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )
    job = PublishJob(
        episode_id=episode.id,
        publisher_target_id=target.id,
        platform=target.platform,
        package_asset_id=package_asset.id,
        status="completed",
        dry_run=False,
        delivery_payload={
            "schema_version": "publish_delivery_payload.v1",
            "title": "Packaged Episode",
            "video_uri": "object://dialecticore/renders/final.mp4",
        },
    )

    qc = PublisherService()._publish_qc(episode, job, package_asset, target)

    assert qc.status == "fail"
    assert qc.severity == QualitySeverity.fail
    assert qc.details["production_manifest_asset_id"] is None
    assert qc.details["production_manifest_checksum_present"] is False
    assert qc.details["production_manifest_schema_version"] is None
    assert {
        issue["issue"]
        for issue in qc.details["issues"]
        if issue["severity"] == "fail"
    } >= {
        "publish_payload_missing_production_manifest_asset_id",
        "publish_payload_missing_production_manifest_checksum",
        "publish_payload_invalid_production_manifest_schema_version",
    }


def test_publish_delivery_qc_fails_live_payload_with_package_mismatch() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )
    job = PublishJob(
        episode_id=episode.id,
        publisher_target_id=target.id,
        platform=target.platform,
        package_asset_id=package_asset.id,
        status="completed",
        dry_run=False,
        delivery_payload={
            "schema_version": "publish_delivery_payload.v1",
            "title": "Packaged Episode",
            "video_uri": "object://dialecticore/renders/final.mp4",
            "package_asset_id": "different-package",
            "package_checksum": "sha256:different",
            "production_manifest_asset_id": "manifest-a",
            "production_manifest_checksum": "sha256:manifest",
            "production_manifest_schema_version": "production_manifest.v1",
        },
    )

    qc = PublisherService()._publish_qc(episode, job, package_asset, target)

    assert qc.status == "fail"
    assert qc.severity == QualitySeverity.fail
    assert qc.details["payload_package_asset_id"] == "different-package"
    assert qc.details["payload_package_checksum_present"] is True
    assert qc.details["package_checksum_matches"] is False
    assert {
        issue["issue"]
        for issue in qc.details["issues"]
        if issue["severity"] == "fail"
    } >= {
        "publish_payload_package_asset_id_mismatch",
        "publish_payload_package_checksum_mismatch",
    }


def test_publisher_service_blocks_live_publish_without_production_manifest() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.append(package_asset)
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    try:
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id=target.id,
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            targets=[target],
        )
    except ValueError as exc:
        assert "production manifest is required before live publishing" in str(exc)
    else:
        raise AssertionError("expected missing production manifest to block live publish")

    assert episode.publish_jobs == []
    assert [
        result
        for result in episode.quality_results
        if result.check_type == "publish_delivery_integrity"
    ] == []
    assert not [
        event for event in episode.audit_events if event.event_type.startswith("publisher.job.")
    ]


def test_publisher_service_blocks_live_publish_without_package_qc() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.extend(
        [
            package_asset,
            _production_manifest_asset(episode.id, package_asset),
        ]
    )
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    with pytest.raises(ValueError, match="YouTube package QC is required"):
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id=target.id,
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            targets=[target],
        )

    assert episode.publish_jobs == []


def test_publisher_service_blocks_live_publish_with_invalid_production_manifest() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.extend(
        [
            package_asset,
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.production_manifest,
                language="en",
                source_entity_type="export_package",
                source_entity_id=str(package_asset.id),
                storage_uri="object://dialecticore/manifests/production.json",
                checksum="sha256:manifest",
                status="completed",
                generation_metadata={"production_manifest": {"schema_version": "draft"}},
            ),
        ]
    )
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    with pytest.raises(ValueError, match="valid production_manifest.v1 asset"):
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id=target.id,
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            targets=[target],
        )

    assert episode.publish_jobs == []


def test_publisher_service_blocks_live_publish_with_unlinked_production_manifest() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    manifest_asset = _production_manifest_asset(episode.id, package_asset)
    manifest_asset.generation_metadata["production_manifest"].pop("delivery_package")
    episode.assets.extend([package_asset, manifest_asset])
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    with pytest.raises(ValueError, match="valid production_manifest.v1 asset"):
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id=target.id,
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            targets=[target],
        )

    assert episode.publish_jobs == []


def test_publisher_service_blocks_live_publish_with_stale_production_manifest_package() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    manifest_asset = _production_manifest_asset(episode.id, package_asset)
    manifest_asset.generation_metadata["production_manifest"]["delivery_package"].update(
        {
            "storage_uri": package_asset.storage_uri,
            "checksum": "sha256:older-package",
        }
    )
    episode.assets.extend([package_asset, manifest_asset])
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    with pytest.raises(ValueError, match="valid production_manifest.v1 asset"):
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id=target.id,
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            targets=[target],
        )

    assert episode.publish_jobs == []


def test_publisher_service_blocks_live_publish_with_stale_manifest_storage_uri() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    manifest_asset = _production_manifest_asset(episode.id, package_asset)
    manifest_asset.generation_metadata["production_manifest"]["delivery_package"].update(
        {
            "storage_uri": "object://dialecticore/exports/older-package.zip",
            "checksum": package_asset.checksum,
        }
    )
    episode.assets.extend([package_asset, manifest_asset])
    episode.quality_results.append(_package_qc(episode.id, package_asset))
    target = PublisherTarget(
        id="live-generic",
        name="Live Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        capabilities={"delivery_path": "/deliveries"},
        health_status="healthy",
    )

    with pytest.raises(ValueError, match="valid production_manifest.v1 asset"):
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id=target.id,
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            targets=[target],
        )

    assert episode.publish_jobs == []


def test_publisher_service_checks_http_target_health(monkeypatch) -> None:
    monkeypatch.setenv("PUBLISHER_TEST_TOKEN", "secret-token")
    target = PublisherTarget(
        id="http-generic",
        name="HTTP Generic Publisher",
        platform="generic",
        adapter_type="http",
        base_url="https://publisher.test",
        credential_reference="env:PUBLISHER_TEST_TOKEN",
        capabilities={"health_path": "/ready"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://publisher.test/ready"
        assert request.headers["authorization"] == "Bearer secret-token"
        return httpx.Response(
            200,
            json={
                "capabilities": {
                    "package_upload": True,
                    "subtitle_upload": True,
                    "api_key": "leaked-publisher-health-api-key",
                    "nested": {"accessToken": "leaked-publisher-health-token"},
                }
            },
        )

    checked = PublisherService(transport=httpx.MockTransport(handler)).check_target_health(target)

    assert checked.health_status == "healthy"
    assert checked.capabilities["dry_run"] is True
    assert checked.capabilities["metadata_upload"] is True
    assert checked.capabilities["package_upload"] is True
    assert checked.capabilities["subtitle_upload"] is True
    assert checked.capabilities["api_key"] == "[redacted]"
    assert checked.capabilities["nested"]["accessToken"] == "[redacted]"
    capabilities_json = json.dumps(checked.capabilities, sort_keys=True)
    assert "leaked-publisher-health-api-key" not in capabilities_json
    assert "leaked-publisher-health-token" not in capabilities_json


def test_publisher_service_blocks_failing_package_qc() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.append(package_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.fail,
            status="fail",
        )
    )

    try:
        PublisherService().publish_package(
            episode,
            PublishRequest(user_id="tester"),
            targets=default_publisher_targets(),
        )
    except ValueError as exc:
        assert "QC blocks publishing" in str(exc)
    else:
        raise AssertionError("expected failing package QC to block publishing")


def test_publisher_service_blocks_failing_package_qc_severity() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    package_asset = _package_asset(episode.id)
    episode.assets.extend([package_asset, _production_manifest_asset(episode.id, package_asset)])
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.fail,
            status="warning",
        )
    )

    with pytest.raises(ValueError, match="failing YouTube package QC blocks publishing"):
        PublisherService().publish_package(
            episode,
            PublishRequest(user_id="tester"),
            targets=default_publisher_targets(),
        )

    assert episode.publish_jobs == []


def _package_asset(episode_id, object_storage_path=None) -> Asset:
    manifest = {
        "title": "Packaged Episode",
        "description": "Ready for delivery.",
        "tags": ["analysis"],
        "language": "en",
        "render_uri": "object://dialecticore/renders/final.mp4",
        "thumbnail_uri": "object://dialecticore/thumbs/final.jpg",
        "subtitles": [{"language": "en", "path": "subtitles/en.vtt"}],
        "chapters": [{"title": "Opening", "start_ms": 0}],
        "evidence_lineage": {
            "citation_links": [{"source_id": "source-a"}],
        },
    }
    metadata = {"youtube_package_manifest": manifest}
    if object_storage_path is not None:
        metadata["object_storage_path"] = str(object_storage_path)
    return Asset(
        episode_id=episode_id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id="render-test",
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata=metadata,
    )


def _production_manifest_asset(episode_id, package_asset: Asset) -> Asset:
    return Asset(
        episode_id=episode_id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
            }
        },
    )


def _package_qc(episode_id, package_asset: Asset, status: str = "pass") -> QualityResult:
    severity = QualitySeverity.pass_ if status == "pass" else QualitySeverity.warning
    return QualityResult(
        episode_id=episode_id,
        target_type="export_package_asset",
        target_id=str(package_asset.id),
        check_type="youtube_package_integrity",
        severity=severity,
        status=status,
        details={"failure_count": 0, "warning_count": 0},
    )


def _youtube_package_path(tmp_path: Path) -> Path:
    package_path = tmp_path / "youtube-package.zip"
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("youtube-package.json", "{}")
        archive.writestr("video/render.mp4", b"video-bytes")
        archive.writestr("thumbnail/thumbnail.jpg", b"thumbnail-bytes")
        archive.writestr("subtitles/en.vtt", "WEBVTT\n\n00:00.000 --> 00:01.000\nText\n")
    return package_path
