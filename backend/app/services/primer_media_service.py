from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import httpx
import yt_dlp
from app.core.config import Settings
from app.domain.enums import AssetType, ProviderType
from app.domain.schemas import Asset, AuditEvent, Episode, PrimerMediaCandidate
from app.services.model_gateway import SecretResolver, auth_headers, openrouter_reasoning_parameters
from app.services.object_storage import ObjectStore, create_object_store


class PrimerMediaService:
    """Find, rank, and acquire attributable source media for an episode topic primer."""

    _MEDIA_META_PATTERN = re.compile(
        r"<meta\b[^>]*(?:property|name)=[\"'](?:og:image|twitter:image|og:video)[\"'][^>]*"
        r"content=[\"'](?P<url>[^\"']+)[\"'][^>]*>",
        flags=re.IGNORECASE,
    )
    _MEDIA_META_REVERSED_PATTERN = re.compile(
        r"<meta\b[^>]*content=[\"'](?P<url>[^\"']+)[\"'][^>]*"
        r"(?:property|name)=[\"'](?:og:image|twitter:image|og:video)[\"'][^>]*>",
        flags=re.IGNORECASE,
    )
    _VIDEO_SOURCE_PATTERN = re.compile(
        r"<(?:video|source)\b[^>]*src=[\"'](?P<url>[^\"']+)[\"']",
        flags=re.IGNORECASE,
    )
    _MEDIA_TYPES = {
        "image/jpeg": (AssetType.image, ".jpg"),
        "image/png": (AssetType.image, ".png"),
        "image/webp": (AssetType.image, ".webp"),
        "video/mp4": (AssetType.video, ".mp4"),
        "video/webm": (AssetType.video, ".webm"),
    }
    _OFFICIAL_SOURCE_TYPES = {
        "official_documentation",
        "government_report",
        "standards_body",
        "industry_report",
    }
    _VIDEO_PLATFORM_HOSTS = {
        "youtube.com",
        "youtu.be",
        "vimeo.com",
        "dailymotion.com",
    }

    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)
        self.transport = transport
        self.secret_resolver = SecretResolver()

    async def discover(
        self, episode: Episode, actor: str | None = None
    ) -> list[PrimerMediaCandidate]:
        policy = episode.definition.media.opening.media_discovery
        if not policy.enabled:
            raise ValueError("primer media discovery is disabled for this episode")
        if not policy.model_endpoint_id or not policy.model_id:
            raise ValueError(
                "primer media discovery requires a configured model endpoint and model ID"
            )
        evidence_sources = self._evidence_sources(episode)
        if not evidence_sources:
            raise ValueError("primer media discovery requires an evidence pack with source URLs")
        web_sources = await asyncio.to_thread(self._search_web_sources, episode)
        sources = self._dedupe_sources([*evidence_sources, *web_sources])
        extracted: list[dict[str, Any]] = []
        for source in sources:
            if self._is_supported_video_platform(source["source_url"]):
                extracted.append(self._platform_video_candidate(source))
            else:
                extracted.extend(await asyncio.to_thread(self._extract_media_from_source, source))
        deduped = self._dedupe_candidates(extracted)
        if not deduped:
            raise ValueError(
                "no downloadable image or video candidates were found in evidence sources"
            )

        ranked, ranking_metadata = await self._rank_candidates(episode, deduped)
        candidates = [
            PrimerMediaCandidate(
                id=f"primer-{uuid4().hex[:12]}",
                media_url=item["media_url"],
                source_url=item["source_url"],
                source_title=item["source_title"],
                source_type=item["source_type"],
                media_type=item["media_type"],
                title=item["title"],
                rationale=item["rationale"],
                rights_status=self._rights_status(item),
                acquisition_method=str(item.get("acquisition_method") or "direct"),
                confidence=float(item["confidence"]),
            )
            for item in ranked[: policy.max_candidates]
        ]
        state = self._state(episode)
        state.update(
            {
                "schema_version": "dialecticore.primer_media.v1",
                "status": "candidates_ready",
                "model_endpoint_id": policy.model_endpoint_id,
                "model_id": policy.model_id,
                "ranking": ranking_metadata,
                "generated_at": datetime.now(UTC).isoformat(),
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            }
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer_media.candidates.discovered",
                actor=actor or "system",
                details={
                    "candidate_count": len(candidates),
                    "evidence_source_count": len(evidence_sources),
                    "web_source_count": len(web_sources),
                    "model_endpoint_id": policy.model_endpoint_id,
                    "model_id": policy.model_id,
                    "acquisition_policy": policy.acquisition_policy,
                    "ranking": ranking_metadata,
                },
            )
        )
        if policy.acquisition_policy == "automatic_official_only":
            for candidate in candidates:
                if candidate.rights_status == "official_source":
                    try:
                        self.acquire(episode, candidate.id, actor="system", automatic=True)
                    except ValueError:
                        # Preserve the failed candidate for operator review without
                        # discarding the rest of a source-discovery run.
                        continue
        return self.candidates(episode)

    def candidates(self, episode: Episode) -> list[PrimerMediaCandidate]:
        raw_candidates = self._state(episode).get("candidates", [])
        if not isinstance(raw_candidates, list):
            return []
        return [
            PrimerMediaCandidate.model_validate(candidate)
            for candidate in raw_candidates
            if isinstance(candidate, dict)
        ]

    def acquire(
        self,
        episode: Episode,
        candidate_id: str,
        actor: str | None = None,
        automatic: bool = False,
    ) -> Asset:
        state = self._state(episode)
        raw_candidates = state.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise ValueError("no primer media candidates are available")
        candidate_index = next(
            (
                index
                for index, item in enumerate(raw_candidates)
                if isinstance(item, dict) and item.get("id") == candidate_id
            ),
            None,
        )
        if candidate_index is None:
            raise ValueError("primer media candidate was not found")
        candidate = PrimerMediaCandidate.model_validate(raw_candidates[candidate_index])
        if candidate.status == "acquired" and candidate.asset_id:
            existing = next(
                (asset for asset in episode.assets if asset.id == candidate.asset_id), None
            )
            if existing is not None:
                return existing
        if automatic and candidate.rights_status != "official_source":
            raise ValueError("automatic acquisition is limited to official-source media")
        try:
            if candidate.acquisition_method == "platform_video":
                payload, content_type, duration_ms = self._download_platform_video(
                    candidate.media_url
                )
            else:
                payload, content_type = self._download_media(candidate.media_url)
                duration_ms = None
            asset_type, extension = self._MEDIA_TYPES[content_type]
            checksum = hashlib.sha256(payload).hexdigest()
            existing_asset = self._existing_opening_media_asset(episode, checksum)
            if existing_asset is not None:
                candidate.status = "acquired"
                candidate.asset_id = existing_asset.id
                raw_candidates[candidate_index] = candidate.model_dump(mode="json")
                state["status"] = "media_acquired"
                episode.audit_events.append(
                    AuditEvent(
                        episode_id=episode.id,
                        event_type="primer_media.candidate.deduplicated",
                        actor=actor or "system",
                        details={
                            "candidate_id": candidate.id,
                            "asset_id": str(existing_asset.id),
                            "source_url": candidate.source_url,
                            "media_url": candidate.media_url,
                            "checksum": existing_asset.checksum,
                        },
                    )
                )
                return existing_asset
            stored = self.object_store.put_bytes(
                key=f"episodes/{episode.id}/primer-media/{checksum[:16]}{extension}",
                payload=payload,
                content_type=content_type,
            )
            asset = Asset(
                episode_id=episode.id,
                asset_type=asset_type,
                language=episode.source_language,
                source_entity_type="episode_opening",
                source_entity_id=str(uuid4()),
                storage_uri=stored.uri,
                mime_type=content_type,
                checksum=stored.checksum,
                duration_ms=duration_ms,
                generation_metadata={
                    "opening_media": True,
                    "opening_media_role": "topic_visual",
                    "title": candidate.title,
                    "source_url": candidate.source_url,
                    "media_url": candidate.media_url,
                    "source_title": candidate.source_title,
                    "source_type": candidate.source_type,
                    "rights_status": candidate.rights_status,
                    "selection_rationale": candidate.rationale,
                    "acquisition_origin": "automatic_official_only"
                    if automatic
                    else "operator_confirmed",
                    "acquisition_method": candidate.acquisition_method,
                    "primer_visual_suitability": {
                        "schema_version": "dialecticore.primer_visual_suitability.v1",
                        "status": "not_assessed",
                        "people_visible": None,
                    },
                    "object_storage_path": str(stored.path),
                    "storage_backend": stored.backend,
                    "render_ready": True,
                },
                status="completed",
            )
            episode.assets.append(asset)
            candidate.status = "acquired"
            candidate.asset_id = asset.id
            raw_candidates[candidate_index] = candidate.model_dump(mode="json")
            state["status"] = "media_acquired"
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="primer_media.candidate.acquired",
                    actor=actor or "system",
                    details={
                        "candidate_id": candidate.id,
                        "asset_id": str(asset.id),
                        "source_url": candidate.source_url,
                        "media_url": candidate.media_url,
                        "rights_status": candidate.rights_status,
                        "automatic": automatic,
                    },
                )
            )
            return asset
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            candidate.status = "failed"
            candidate.failure_reason = str(exc)[:240]
            raw_candidates[candidate_index] = candidate.model_dump(mode="json")
            raise ValueError(f"primer media download failed: {candidate.failure_reason}") from exc

    def import_operator_source(
        self,
        episode: Episode,
        source_url: str,
        *,
        title: str = "",
        actor: str | None = None,
    ) -> Asset:
        """Download a producer-selected public media URL into the private source library."""
        normalized_url = source_url.strip()
        if not normalized_url:
            raise ValueError("source URL is required")
        try:
            if self._is_supported_video_platform(normalized_url):
                payload, content_type, duration_ms = self._download_platform_video(normalized_url)
                acquisition_method = "platform_video"
            else:
                payload, content_type = self._download_media(normalized_url)
                duration_ms = None
                acquisition_method = "direct"
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError(f"source import failed: {str(exc)[:240]}") from exc
        asset_type, extension = self._MEDIA_TYPES[content_type]
        checksum = hashlib.sha256(payload).hexdigest()
        existing_asset = self._existing_opening_media_asset(episode, checksum)
        if existing_asset is not None:
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="primer_media.source.deduplicated",
                    actor=actor or "web-ui",
                    details={
                        "asset_id": str(existing_asset.id),
                        "source_url": normalized_url,
                        "checksum": existing_asset.checksum,
                    },
                )
            )
            return existing_asset
        stored = self.object_store.put_bytes(
            key=f"episodes/{episode.id}/primer-media/{checksum[:16]}{extension}",
            payload=payload,
            content_type=content_type,
        )
        source_host = urlparse(normalized_url).hostname or "source"
        asset = Asset(
            episode_id=episode.id,
            asset_type=asset_type,
            language=episode.source_language,
            source_entity_type="episode_opening",
            source_entity_id=str(uuid4()),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            duration_ms=duration_ms,
            generation_metadata={
                "opening_media": True,
                "opening_media_role": "topic_visual",
                "title": title.strip() or f"Imported media from {source_host}",
                "source_url": normalized_url,
                "media_url": normalized_url,
                "source_title": title.strip() or source_host,
                "source_type": "operator_link",
                "rights_status": "editorial_review_required",
                "acquisition_origin": "operator_link",
                "acquisition_method": acquisition_method,
                "primer_visual_suitability": {
                    "schema_version": "dialecticore.primer_visual_suitability.v1",
                    "status": "not_assessed",
                    "people_visible": None,
                },
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
                "render_ready": True,
            },
            status="completed",
        )
        episode.assets.append(asset)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="primer_media.source.imported",
                actor=actor or "web-ui",
                details={
                    "asset_id": str(asset.id),
                    "asset_type": asset.asset_type.value,
                    "source_url": normalized_url,
                    "acquisition_method": acquisition_method,
                    "checksum": stored.checksum,
                },
            )
        )
        return asset

    @staticmethod
    def _existing_opening_media_asset(episode: Episode, checksum: str) -> Asset | None:
        expected_checksum = f"sha256:{checksum}"
        return next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type in {AssetType.image, AssetType.video}
                and asset.status == "completed"
                and asset.source_entity_type == "episode_opening"
                and asset.generation_metadata.get("opening_media") is True
                and asset.checksum == expected_checksum
            ),
            None,
        )

    def _evidence_sources(self, episode: Episode) -> list[dict[str, str]]:
        evidence_asset = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
            ),
            None,
        )
        if evidence_asset is None:
            return []
        pack = evidence_asset.generation_metadata.get("evidence_pack")
        if not isinstance(pack, dict):
            return []
        sources = pack.get("source_index", [])
        if not isinstance(sources, list):
            return []
        return [
            {
                "source_url": str(source.get("uri") or "").strip(),
                "source_title": str(source.get("title") or "Source").strip(),
                "source_type": str(source.get("source_type") or "web_page").strip(),
            }
            for source in sources
            if isinstance(source, dict) and self._is_public_http_url(str(source.get("uri") or ""))
        ]

    def _extract_media_from_source(self, source: dict[str, str]) -> list[dict[str, Any]]:
        source_url = source["source_url"]
        try:
            request = Request(
                source_url,
                headers={
                    "User-Agent": "DialectiCorePrimerMediaBot/0.1",
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                },
            )
            with urlopen(
                request, timeout=self.settings.primer_media_source_timeout_seconds
            ) as response:
                if not self._is_public_http_url(response.geturl()):
                    return []
                content_type = response.headers.get("content-type") or ""
                if "html" not in content_type.lower():
                    return []
                payload = response.read(self.settings.primer_media_source_max_bytes + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            return []
        if len(payload) > self.settings.primer_media_source_max_bytes:
            payload = payload[: self.settings.primer_media_source_max_bytes]
        html = payload.decode("utf-8", errors="replace")
        primary_urls = [
            *[match.group("url") for match in self._MEDIA_META_PATTERN.finditer(html)],
            *[match.group("url") for match in self._MEDIA_META_REVERSED_PATTERN.finditer(html)],
        ]
        embedded_video_urls = [
            match.group("url") for match in self._VIDEO_SOURCE_PATTERN.finditer(html)
        ]
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_candidate(media_url: str, media_type: str, role: str) -> bool:
            resolved = urljoin(source_url, media_url.strip())
            if not self._is_public_http_url(resolved) or resolved in seen:
                return False
            seen.add(resolved)
            candidates.append(
                {
                    **source,
                    "media_url": resolved,
                    "media_type": media_type,
                    "title": f"{source['source_title']} ({role})",
                    "rationale": (
                        f"{role.capitalize()} selected from the source page to establish "
                        "the episode topic context."
                    ),
                    "acquisition_method": "direct",
                    "confidence": 0.5,
                }
            )
            return True

        # Social metadata represents the page's editorially chosen primary visual.
        for media_url in primary_urls:
            resolved = urljoin(source_url, media_url.strip())
            if re.search(r"\.(?:mp4|webm)(?:$|[?#])", resolved, re.I):
                add_candidate(media_url, "video", "primary video")
            else:
                add_candidate(media_url, "image", "primary image")

        # A page may contain related-video widgets. Keep only its first downloadable
        # embedded video; discovery should provide variety by finding other sources.
        for media_url in embedded_video_urls:
            if not re.search(r"\.(?:mp4|webm)(?:$|[?#])", media_url, re.I):
                continue
            if add_candidate(media_url, "video", "embedded video"):
                break

        return candidates

    def _search_web_sources(self, episode: Episode) -> list[dict[str, str]]:
        """Use the configured research search provider to widen primer media discovery."""
        if not getattr(self.settings, "primer_media_web_search_enabled", True):
            return []
        template = getattr(self.settings, "research_discovery_url_template", None)
        sources: list[dict[str, str]] = []
        if getattr(self.settings, "research_discovery_enabled", False) and template:
            for query in self._web_search_queries(episode):
                discovery_url = self._discovery_url(template, query)
                try:
                    request = Request(
                        discovery_url,
                        headers={
                            "User-Agent": "DialectiCorePrimerMediaBot/0.2",
                            "Accept": "application/json,text/html;q=0.9,*/*;q=0.1",
                        },
                    )
                    with urlopen(
                        request, timeout=self.settings.primer_media_source_timeout_seconds
                    ) as response:
                        payload = response.read(self.settings.primer_media_source_max_bytes + 1)
                        content_type = response.headers.get("content-type") or ""
                except (HTTPError, URLError, TimeoutError, OSError):
                    continue
                if len(payload) > self.settings.primer_media_source_max_bytes:
                    continue
                records = self._search_records(
                    payload.decode("utf-8", errors="replace"), content_type
                )
                for record in records:
                    source_url = str(record.get("url") or "").strip()
                    if not self._is_public_http_url(source_url):
                        continue
                    sources.append(
                        {
                            "source_url": source_url,
                            "source_title": str(
                                record.get("title") or self._title_from_url(source_url)
                            ),
                            "source_type": self._source_type_for_url(source_url),
                        }
                    )
        if not any(self._is_supported_video_platform(source["source_url"]) for source in sources):
            sources.extend(self._search_platform_video_sources(episode))
        return sources

    def _search_platform_video_sources(self, episode: Episode) -> list[dict[str, str]]:
        """Fallback to bounded public YouTube search when the shared web index lacks video."""
        if not getattr(self.settings, "primer_media_platform_search_enabled", True):
            return []
        sources: list[dict[str, str]] = []
        result_count = self.settings.primer_media_platform_search_results_per_query
        for query in self._web_search_queries(episode):
            try:
                with yt_dlp.YoutubeDL(
                    {"quiet": True, "no_warnings": True, "skip_download": True}
                ) as extractor:
                    result = extractor.extract_info(
                        f"ytsearch{result_count}:{query}", download=False
                    )
            except (yt_dlp.utils.DownloadError, OSError, ValueError):
                continue
            entries = result.get("entries", []) if isinstance(result, dict) else []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                source_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
                if not self._is_supported_video_platform(source_url):
                    continue
                sources.append(
                    {
                        "source_url": source_url,
                        "source_title": str(entry.get("title") or self._title_from_url(source_url)),
                        "source_type": "video_platform",
                    }
                )
        return self._dedupe_sources(sources)

    def _web_search_queries(self, episode: Episode) -> list[str]:
        topic = " ".join((episode.title or episode.definition.topic.central_question).split())
        dimensions = episode.definition.topic.required_dimensions
        raw = [
            f"{topic} video",
            f"{topic} Nachrichten Video",
            *(f"{topic} {dimension} video" for dimension in dimensions),
        ]
        queries: list[str] = []
        for query in raw:
            normalized = " ".join(query.split())
            if normalized and normalized not in queries:
                queries.append(normalized)
            if len(queries) >= self.settings.primer_media_web_search_max_queries:
                break
        return queries

    @staticmethod
    def _discovery_url(template: str, query: str) -> str:
        encoded = quote_plus(query)
        if "{query}" in template:
            return template.replace("{query}", encoded)
        separator = "&" if "?" in template else "?"
        return f"{template}{separator}q={encoded}"

    def _search_records(self, payload_text: str, content_type: str) -> list[dict[str, object]]:
        if "json" not in content_type.lower() and not payload_text.lstrip().startswith(("{", "[")):
            return []
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = payload.get("results", [])
        else:
            records = []
        if not isinstance(records, list):
            return []
        selected: list[dict[str, object]] = []
        for record in records[: self.settings.primer_media_web_search_max_results_per_query]:
            if not isinstance(record, dict):
                continue
            selected.append(
                {
                    "url": record.get("url") or record.get("uri") or record.get("link"),
                    "title": record.get("title") or record.get("name"),
                }
            )
        return selected

    def _platform_video_candidate(self, source: dict[str, str]) -> dict[str, Any]:
        return {
            **source,
            "media_url": source["source_url"],
            "media_type": "video",
            "title": source["source_title"],
            "rationale": (
                "Public video result found through the configured topic search. Review source "
                "and rights before downloading an excerpt."
            ),
            "acquisition_method": "platform_video",
            "confidence": 0.65,
        }

    @classmethod
    def _is_supported_video_platform(cls, value: str) -> bool:
        host = (urlparse(value).hostname or "").lower().removeprefix("www.")
        return host in cls._VIDEO_PLATFORM_HOSTS

    @staticmethod
    def _title_from_url(value: str) -> str:
        host = urlparse(value).hostname or "Source"
        return host.removeprefix("www.")

    def _source_type_for_url(self, value: str) -> str:
        if self._is_supported_video_platform(value):
            return "video_platform"
        host = (urlparse(value).hostname or "").lower().removeprefix("www.")
        if (
            host.endswith(".gov")
            or host.endswith(".europa.eu")
            or host
            in {
                "bundesregierung.de",
                "bundesnetzagentur.de",
                "bmds.bund.de",
                "bmwk.de",
                "destatis.de",
                "umweltbundesamt.de",
            }
        ):
            return "government_report"
        return "web_page"

    @staticmethod
    def _dedupe_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for source in sources:
            key = source["source_url"].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(source)
        return unique

    async def _rank_candidates(
        self,
        episode: Episode,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        policy = episode.definition.media.opening.media_discovery
        if not policy.model_endpoint_id or not policy.model_id:
            return self._deterministic_rank(candidates), {"mode": "deterministic_unconfigured"}
        endpoint = next(
            (item for item in episode.model_endpoints if item.id == policy.model_endpoint_id),
            None,
        )
        if endpoint is None or not endpoint.enabled:
            raise ValueError("configured primer media model endpoint is unavailable")
        if endpoint.provider_type == ProviderType.mock:
            return self._deterministic_rank(candidates), {"mode": "deterministic_mock"}
        if endpoint.provider_type != ProviderType.openai_compatible or not endpoint.base_url:
            raise ValueError(
                "primer media discovery currently requires an OpenAI-compatible model endpoint"
            )
        prompt_candidates = [
            {
                "media_url": item["media_url"],
                "source_url": item["source_url"],
                "source_title": item["source_title"],
                "source_type": item["source_type"],
                "media_type": item["media_type"],
                "acquisition_method": item.get("acquisition_method", "direct"),
            }
            for item in candidates
        ]
        prompt = {
            "role": (
                "You are selecting attributable visual media for an evidence-led "
                "talk-show topic primer."
            ),
            "episode_topic": episode.definition.topic.central_question,
            "rules": [
                "Select only supplied candidates; never invent URLs.",
                "Prefer official or primary-source media, then contextually useful reporting.",
                (
                    "Preserve the editor's requested source mix; do not add media merely to "
                    "meet a quota."
                ),
                (
                    "The primer uses people-free editorial imagery. Prefer infrastructure, maps, "
                    "documents, charts, landscapes, machinery, products, and environmental "
                    "footage; "
                    "avoid interviews, presenters, crowds, faces, and visible people."
                    if episode.definition.media.opening.visual_planner.exclude_people
                    else "People may appear only where contextually necessary."
                ),
                "Do not infer copyright permission; explain editorial relevance only.",
                (
                    "Return JSON with a candidates array containing media_url, title, rationale, "
                    "confidence."
                ),
            ],
            "candidates": prompt_candidates,
        }
        payload = {
            "model": policy.model_id,
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": json.dumps(prompt)},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
            **openrouter_reasoning_parameters(endpoint),
        }
        try:
            async with httpx.AsyncClient(
                base_url=endpoint.base_url.rstrip("/"),
                timeout=endpoint.default_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/chat/completions",
                    json=payload,
                    headers=auth_headers(endpoint, self.secret_resolver),
                )
                response.raise_for_status()
            raw = response.json()
            content = raw["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            selected = parsed.get("candidates", []) if isinstance(parsed, dict) else parsed
        except (
            httpx.HTTPError,
            AttributeError,
            KeyError,
            RuntimeError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            return self._deterministic_rank(candidates), {
                "mode": "deterministic_fallback",
                "reason": type(exc).__name__,
            }
        if not isinstance(selected, list):
            return self._deterministic_rank(candidates), {
                "mode": "deterministic_fallback",
                "reason": "missing_candidate_list",
            }
        by_url = {item["media_url"]: item for item in candidates}
        ranked: list[dict[str, Any]] = []
        for selection in selected:
            if not isinstance(selection, dict):
                continue
            original = by_url.get(str(selection.get("media_url") or ""))
            if original is None:
                continue
            ranked.append(
                {
                    **original,
                    "title": str(selection.get("title") or original["title"])[:512],
                    "rationale": str(selection.get("rationale") or original["rationale"])[:1200],
                    "confidence": min(1.0, max(0.0, float(selection.get("confidence", 0.5)))),
                }
            )
        if ranked:
            return ranked, {"mode": "model"}
        return self._deterministic_rank(candidates), {
            "mode": "deterministic_fallback",
            "reason": "no_valid_model_selections",
        }

    def _deterministic_rank(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            candidates,
            key=lambda item: (
                item.get("source_type") not in self._OFFICIAL_SOURCE_TYPES,
                item.get("media_type") != "video",
                -float(item.get("confidence", 0.0)),
                item["media_url"],
            ),
        )

    def _download_media(self, media_url: str) -> tuple[bytes, str]:
        if not self._is_public_http_url(media_url):
            raise ValueError("media URL must use a public HTTP(S) host")
        request = Request(
            media_url,
            headers={
                "User-Agent": "DialectiCorePrimerMediaBot/0.1",
                "Accept": "image/jpeg,image/png,image/webp,video/mp4,video/webm",
            },
        )
        with urlopen(
            request, timeout=self.settings.primer_media_download_timeout_seconds
        ) as response:
            if not self._is_public_http_url(response.geturl()):
                raise ValueError("media redirect must use a public HTTP(S) host")
            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type not in self._MEDIA_TYPES:
                raise ValueError(f"unsupported primer media type: {content_type or 'unknown'}")
            content_length = self._content_length(response.headers.get("content-length"))
            if (
                content_length is not None
                and content_length > self.settings.primer_media_max_download_bytes
            ):
                raise ValueError(self._download_size_limit_message(content_length))
            payload = response.read(self.settings.primer_media_max_download_bytes + 1)
        if len(payload) > self.settings.primer_media_max_download_bytes:
            raise ValueError(self._download_size_limit_message(len(payload)))
        if not payload:
            raise ValueError("primer media download is empty")
        return payload, content_type

    def _download_platform_video(self, media_url: str) -> tuple[bytes, str, int | None]:
        """Download an operator-approved public platform video with hard bounds."""
        if not self._is_supported_video_platform(media_url) or not self._is_public_http_url(
            media_url
        ):
            raise ValueError("unsupported or non-public video platform URL")
        with tempfile.TemporaryDirectory(prefix="dialecticore-primer-video-") as directory:
            output_template = str(Path(directory) / "source.%(ext)s")
            limit_mebibytes = max(
                1,
                self.settings.primer_media_max_download_bytes // (1024 * 1024),
            )
            options = {
                "format": (
                    f"best[ext=mp4][height<=1080][filesize<{limit_mebibytes}M]/"
                    f"best[ext=webm][height<=1080][filesize<{limit_mebibytes}M]/"
                    "best[height<=1080]"
                ),
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "max_filesize": self.settings.primer_media_max_download_bytes,
                "socket_timeout": self.settings.primer_media_download_timeout_seconds,
                "restrictfilenames": True,
            }
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    metadata = downloader.extract_info(media_url, download=True)
                    downloaded_path = Path(downloader.prepare_filename(metadata))
            except (yt_dlp.utils.DownloadError, OSError, ValueError) as exc:
                raise ValueError(f"platform video download failed: {type(exc).__name__}") from exc
            if not downloaded_path.exists():
                files = list(Path(directory).glob("source.*"))
                downloaded_path = files[0] if len(files) == 1 else Path()
            content_type = {".mp4": "video/mp4", ".webm": "video/webm"}.get(
                downloaded_path.suffix.lower()
            )
            if content_type is None or not downloaded_path.exists():
                raise ValueError("platform video download did not produce an MP4 or WebM file")
            payload = downloaded_path.read_bytes()
            if not payload or len(payload) > self.settings.primer_media_max_download_bytes:
                raise ValueError(self._download_size_limit_message(len(payload)))
            duration = metadata.get("duration") if isinstance(metadata, dict) else None
            duration_ms = (
                round(float(duration) * 1000) if isinstance(duration, int | float) else None
            )
            return payload, content_type, duration_ms

    @staticmethod
    def _content_length(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    def _download_size_limit_message(self, size_bytes: int) -> str:
        size_mebibytes = size_bytes / (1024 * 1024)
        limit_mebibytes = self.settings.primer_media_max_download_bytes / (1024 * 1024)
        return (
            f"primer media download is {size_mebibytes:.1f} MiB; "
            f"the configured limit is {limit_mebibytes:.0f} MiB"
        )

    def _rights_status(self, item: dict[str, Any]) -> str:
        source_host = urlparse(item["source_url"]).hostname
        media_host = urlparse(item["media_url"]).hostname
        if item["source_type"] in self._OFFICIAL_SOURCE_TYPES and source_host == media_host:
            return "official_source"
        if item["source_type"] in {"news_article", "web_page", "video_platform"}:
            return "editorial_review_required"
        return "unknown"

    def _dedupe_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = candidate["media_url"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    def _state(self, episode: Episode) -> dict[str, Any]:
        state = episode.workflow_control.get("primer_media")
        if not isinstance(state, dict):
            state = {}
            episode.workflow_control["primer_media"] = state
        return state

    @staticmethod
    def _is_public_http_url(value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        try:
            addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        for address in addresses:
            host = address[4][0]
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                continue
            if not ip.is_global:
                return False
        return True
