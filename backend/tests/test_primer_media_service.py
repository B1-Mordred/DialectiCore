import asyncio
import json

import httpx
import pytest
from app.core.config import Settings
from app.domain.defaults import (
    default_model_endpoints,
    default_participants,
    openrouter_model_endpoint,
)
from app.domain.enums import AssetType
from app.domain.schemas import Asset, EpisodeCreateRequest
from app.infrastructure.repository import EpisodeRepository
from app.services.primer_media_service import PrimerMediaService


def _episode_with_evidence_pack():
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition={
                "title": "Evidence-led primer",
                "topic": {
                    "central_question": "What changed in the public AI policy proposal?",
                    "required_dimensions": ["context", "impact"],
                    "exclusions": [],
                },
                "format": {"target_duration_minutes": 3, "participant_count": 4},
                "participants": [
                    {"participant_profile_id": "host", "role": "moderator"},
                    {"participant_profile_id": "optimist", "role": "panelist"},
                    {"participant_profile_id": "skeptic", "role": "panelist"},
                    {"participant_profile_id": "practitioner", "role": "panelist"},
                ],
                "media": {
                    "opening": {
                        "media_discovery": {
                            "enabled": True,
                            "model_endpoint_id": "mock",
                            "model_id": "deterministic-ranking",
                        }
                    }
                },
            },
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.evidence_pack,
            source_entity_type="research",
            source_entity_id="evidence-pack",
            status="completed",
            generation_metadata={
                "evidence_pack": {
                    "source_index": [
                        {
                            "uri": "https://agency.example/policy-update",
                            "title": "Agency policy update",
                            "source_type": "official_documentation",
                        }
                    ]
                }
            },
        )
    )
    return episode


def test_discovers_only_evidence_page_media_and_marks_official_source(
    tmp_path, monkeypatch
) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(
        Settings(
            object_storage_local_path=str(tmp_path / "object-store"),
            primer_media_web_search_enabled=False,
        )
    )
    monkeypatch.setattr(
        service,
        "_evidence_sources",
        lambda _: [
            {
                "source_url": "https://agency.example/policy-update",
                "source_title": "Agency policy update",
                "source_type": "official_documentation",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_extract_media_from_source",
        lambda source: [
            {
                **source,
                "media_url": "https://agency.example/assets/policy-still.webp",
                "media_type": "image",
                "title": "Policy briefing visual",
                "rationale": "Source page media that explains the update.",
                "confidence": 0.7,
            }
        ],
    )

    candidates = asyncio.run(service.discover(episode, actor="tester"))

    assert len(candidates) == 1
    assert candidates[0].media_url == "https://agency.example/assets/policy-still.webp"
    assert candidates[0].rights_status == "official_source"
    assert episode.workflow_control["primer_media"]["model_endpoint_id"] == "mock"
    assert episode.audit_events[-1].event_type == "primer_media.candidates.discovered"


def test_acquires_selected_candidate_as_renderable_opening_asset(tmp_path, monkeypatch) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(
        Settings(
            object_storage_local_path=str(tmp_path / "object-store"),
            primer_media_web_search_enabled=False,
        )
    )
    monkeypatch.setattr(
        service,
        "_evidence_sources",
        lambda _: [
            {
                "source_url": "https://agency.example/policy-update",
                "source_title": "Agency policy update",
                "source_type": "official_documentation",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_evidence_sources",
        lambda _: [
            {
                "source_url": "https://agency.example/policy-update",
                "source_title": "Agency policy update",
                "source_type": "official_documentation",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_extract_media_from_source",
        lambda source: [
            {
                **source,
                "media_url": "https://agency.example/assets/policy-still.webp",
                "media_type": "image",
                "title": "Policy briefing visual",
                "rationale": "Source page media that explains the update.",
                "confidence": 0.7,
            }
        ],
    )
    candidate = asyncio.run(service.discover(episode))[0]
    monkeypatch.setattr(service, "_download_media", lambda _: (b"webp-image", "image/webp"))

    asset = service.acquire(episode, candidate.id, actor="tester")

    assert asset.asset_type == AssetType.image
    assert asset.source_entity_type == "episode_opening"
    assert asset.generation_metadata["opening_media"] is True
    assert asset.generation_metadata["source_url"] == "https://agency.example/policy-update"
    assert service.candidates(episode)[0].status == "acquired"
    assert service.candidates(episode)[0].asset_id == asset.id


def test_acquiring_duplicate_candidate_reuses_existing_opening_asset(tmp_path, monkeypatch) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(
        Settings(
            object_storage_local_path=str(tmp_path / "object-store"),
            primer_media_web_search_enabled=False,
        )
    )
    monkeypatch.setattr(
        service,
        "_evidence_sources",
        lambda _: [
            {
                "source_url": "https://agency.example/policy-update",
                "source_title": "Agency policy update",
                "source_type": "official_documentation",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_extract_media_from_source",
        lambda source: [
            {
                **source,
                "media_url": "https://agency.example/assets/policy-one.webp",
                "media_type": "image",
                "title": "Policy briefing visual one",
                "rationale": "First reference to the public source image.",
                "confidence": 0.7,
            },
            {
                **source,
                "media_url": "https://agency.example/assets/policy-two.webp",
                "media_type": "image",
                "title": "Policy briefing visual two",
                "rationale": "Second reference resolves to identical media bytes.",
                "confidence": 0.7,
            },
        ],
    )
    candidates = asyncio.run(service.discover(episode))
    monkeypatch.setattr(service, "_download_media", lambda _: (b"same-image", "image/webp"))

    first = service.acquire(episode, candidates[0].id, actor="tester")
    second = service.acquire(episode, candidates[1].id, actor="tester")

    assert first.id == second.id
    assert (
        len(
            [
                asset
                for asset in episode.assets
                if asset.generation_metadata.get("opening_media") is True
            ]
        )
        == 1
    )
    assert service.candidates(episode)[1].asset_id == first.id
    assert episode.audit_events[-1].event_type == "primer_media.candidate.deduplicated"


def test_discovers_public_web_video_candidates_alongside_evidence_media(
    tmp_path, monkeypatch
) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    monkeypatch.setattr(
        service,
        "_evidence_sources",
        lambda _: [
            {
                "source_url": "https://agency.example/policy-update",
                "source_title": "Agency policy update",
                "source_type": "official_documentation",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_search_web_sources",
        lambda _: [
            {
                "source_url": "https://www.youtube.com/watch?v=topic-video",
                "source_title": "Public broadcaster explainer",
                "source_type": "video_platform",
            }
        ],
    )
    monkeypatch.setattr(service, "_extract_media_from_source", lambda _: [])

    candidates = asyncio.run(service.discover(episode, actor="tester"))

    assert len(candidates) == 1
    assert candidates[0].media_type == "video"
    assert candidates[0].acquisition_method == "platform_video"
    assert candidates[0].rights_status == "editorial_review_required"
    assert episode.audit_events[-1].details["web_source_count"] == 1


def test_acquires_reviewed_platform_video_with_bounded_extractor(tmp_path, monkeypatch) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    monkeypatch.setattr(
        service,
        "_evidence_sources",
        lambda _: [
            {
                "source_url": "https://agency.example/policy-update",
                "source_title": "Agency policy update",
                "source_type": "official_documentation",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_search_web_sources",
        lambda _: [
            {
                "source_url": "https://www.youtube.com/watch?v=topic-video",
                "source_title": "Public broadcaster explainer",
                "source_type": "video_platform",
            }
        ],
    )
    monkeypatch.setattr(service, "_extract_media_from_source", lambda _: [])
    monkeypatch.setattr(
        service,
        "_download_platform_video",
        lambda _: (b"mp4-video", "video/mp4", 42_000),
    )
    candidate = asyncio.run(service.discover(episode))[0]

    asset = service.acquire(episode, candidate.id, actor="tester")

    assert asset.asset_type == AssetType.video
    assert asset.duration_ms == 42_000
    assert asset.generation_metadata["acquisition_method"] == "platform_video"


def test_extracts_primary_visual_and_one_embedded_video_per_source(tmp_path, monkeypatch) -> None:
    service = PrimerMediaService(Settings(object_storage_local_path=str(tmp_path / "object-store")))

    class Response:
        headers = {"content-type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self) -> str:
            return "https://news.example/topic"

        def read(self, _: int) -> bytes:
            return b"""
                <meta content=\"https://cdn.example/cover.jpg\" property=\"og:image\">
                <meta content=\"https://cdn.example/cover.jpg\" name=\"twitter:image\">
                <source src=\"https://cdn.example/first.mp4\" type=\"video/mp4\">
                <source src=\"https://cdn.example/related.mp4\" type=\"video/mp4\">
                <source src=\"https://cdn.example/stream.m3u8\" type=\"application/x-mpegURL\">
            """

    monkeypatch.setattr(
        "app.services.primer_media_service.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(service, "_is_public_http_url", lambda _: True)

    candidates = service._extract_media_from_source(
        {
            "source_url": "https://news.example/topic",
            "source_title": "Data-centre report",
            "source_type": "news_article",
        }
    )

    assert [(candidate["media_type"], candidate["media_url"]) for candidate in candidates] == [
        ("image", "https://cdn.example/cover.jpg"),
        ("video", "https://cdn.example/first.mp4"),
    ]
    assert candidates[0]["title"] == "Data-centre report (primary image)"
    assert candidates[1]["title"] == "Data-centre report (embedded video)"


def test_rejects_announced_oversized_media_before_reading_payload(tmp_path, monkeypatch) -> None:
    service = PrimerMediaService(
        Settings(
            object_storage_local_path=str(tmp_path / "object-store"),
            primer_media_max_download_bytes=50 * 1024 * 1024,
        )
    )

    class Response:
        headers = {
            "content-type": "video/mp4",
            "content-length": str(64 * 1024 * 1024),
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self) -> str:
            return "https://media.example/clip.mp4"

        def read(self, _: int) -> bytes:
            raise AssertionError("oversized response should be rejected before reading")

    monkeypatch.setattr(
        "app.services.primer_media_service.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(service, "_is_public_http_url", lambda _: True)

    with pytest.raises(
        ValueError,
        match=r"64\.0 MiB; the configured limit is 50 MiB",
    ):
        service._download_media("https://media.example/clip.mp4")


def test_primer_media_defaults_allow_high_quality_source_clips() -> None:
    settings = Settings()

    assert settings.primer_media_max_download_bytes == 384 * 1024 * 1024
    assert settings.primer_media_download_timeout_seconds == 90


def test_accepts_root_array_from_media_ranking_model(tmp_path) -> None:
    episode = _episode_with_evidence_pack()
    endpoint = openrouter_model_endpoint().model_copy(update={"credential_reference": None})
    episode.model_endpoints.append(endpoint)
    episode.definition.media.opening.media_discovery.model_endpoint_id = "openrouter"
    episode.definition.media.opening.media_discovery.model_id = "provider/video-ranker"
    candidates = [
        {
            "media_url": "https://www.youtube.com/watch?v=topic-video",
            "source_url": "https://www.youtube.com/watch?v=topic-video",
            "source_title": "Public broadcaster explainer",
            "source_type": "video_platform",
            "media_type": "video",
            "title": "Public broadcaster explainer",
            "rationale": "Relevant source video.",
            "confidence": 0.65,
            "acquisition_method": "platform_video",
        }
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(candidates)}}]},
        )

    service = PrimerMediaService(
        Settings(object_storage_local_path=str(tmp_path / "object-store")),
        transport=httpx.MockTransport(handler),
    )
    ranked, metadata = asyncio.run(service._rank_candidates(episode, candidates))

    assert metadata == {"mode": "model"}
    assert ranked[0]["media_url"] == candidates[0]["media_url"]


def test_media_ranking_preserves_the_model_selected_range_without_forcing_videos(
    tmp_path,
) -> None:
    episode = _episode_with_evidence_pack()
    endpoint = openrouter_model_endpoint().model_copy(update={"credential_reference": None})
    episode.model_endpoints.append(endpoint)
    episode.definition.media.opening.media_discovery.model_endpoint_id = "openrouter"
    episode.definition.media.opening.media_discovery.model_id = "provider/video-ranker"
    image = {
        "media_url": "https://source.example/image.jpg",
        "source_url": "https://source.example/article",
        "source_title": "Official chart",
        "source_type": "official_documentation",
        "media_type": "image",
        "title": "Official chart",
        "rationale": "Directly explains the topic.",
        "confidence": 0.9,
    }
    video_one = {
        "media_url": "https://video.example/one",
        "source_url": "https://video.example/one",
        "source_title": "Video one",
        "source_type": "video_platform",
        "media_type": "video",
        "title": "Video one",
        "rationale": "Relevant video.",
        "confidence": 0.8,
    }
    video_two = {
        "media_url": "https://video.example/two",
        "source_url": "https://video.example/two",
        "source_title": "Video two",
        "source_type": "video_platform",
        "media_type": "video",
        "title": "Video two",
        "rationale": "Relevant video.",
        "confidence": 0.7,
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps([image])}}]},
        )

    service = PrimerMediaService(
        Settings(object_storage_local_path=str(tmp_path / "object-store")),
        transport=httpx.MockTransport(handler),
    )
    ranked, metadata = asyncio.run(service._rank_candidates(episode, [image, video_one, video_two]))

    assert metadata == {"mode": "model"}
    assert ranked == [image]


def test_imports_operator_selected_direct_media_into_the_private_source_library(
    tmp_path, monkeypatch
) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    monkeypatch.setattr(service, "_download_media", lambda _: (b"webp-image", "image/webp"))

    asset = service.import_operator_source(
        episode,
        "https://news.example/visuals/public-policy.webp",
        title="Policy graphic",
        actor="tester",
    )

    assert asset in episode.assets
    assert asset.asset_type == AssetType.image
    assert asset.generation_metadata["opening_media"] is True
    assert asset.generation_metadata["acquisition_origin"] == "operator_link"
    assert (
        asset.generation_metadata["source_url"] == "https://news.example/visuals/public-policy.webp"
    )
    assert episode.audit_events[-1].event_type == "primer_media.source.imported"


def test_uses_platform_search_when_shared_web_search_has_no_video(tmp_path, monkeypatch) -> None:
    episode = _episode_with_evidence_pack()
    service = PrimerMediaService(
        Settings(
            object_storage_local_path=str(tmp_path / "object-store"),
            research_discovery_enabled=False,
        )
    )
    expected = [
        {
            "source_url": "https://www.youtube.com/watch?v=topic-video",
            "source_title": "Public broadcaster explainer",
            "source_type": "video_platform",
        }
    ]
    monkeypatch.setattr(service, "_search_platform_video_sources", lambda _: expected)

    assert service._search_web_sources(episode) == expected
