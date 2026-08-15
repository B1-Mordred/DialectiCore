from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity
from app.domain.schemas import (
    Approval,
    Asset,
    AuditEvent,
    Episode,
    EvidenceClaim,
    EvidencePack,
    EvidenceSource,
    QualityResult,
    ResearchBuildRequest,
    ResearchClaimQcRequest,
    ResearchRetrievalTarget,
    ResearchSourceInput,
    ResearchSourceReviewRequest,
    TranscriptVersion,
)
from app.services.object_storage import ObjectStore, create_object_store
from app.services.production_control_service import ProductionControlService


class ResearchService:
    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)
        self.production_control = ProductionControlService(settings)

    def build_evidence_pack(
        self,
        episode: Episode,
        request: ResearchBuildRequest,
    ) -> Episode:
        existing = self._latest_evidence_pack_asset(episode)
        if existing is not None and not request.regenerate:
            raise ValueError("evidence pack already exists for episode")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        previous_status = episode.status
        episode.status = EpisodeStatus.researching
        self.production_control.record_stage(
            episode,
            EpisodeStatus.researching,
            "research_service.build_evidence_pack",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.researching.value},
            )
        )

        discovered_targets, discovery_tool_log = self._discover_retrieval_targets(
            episode,
            request,
        )
        retrieval_targets = self._dedupe_retrieval_targets(
            [*request.retrieval_targets, *discovered_targets]
        )
        retrieved_sources, retrieval_tool_log = self._retrieve_sources(retrieval_targets)
        pack = self._build_pack(episode, [*request.sources, *retrieved_sources])
        payload = pack.model_dump_json(indent=2).encode("utf-8")
        stored = self.object_store.put_bytes(
            key=f"research/{episode.id}/{pack.id}.evidence-pack.json",
            payload=payload,
            content_type="application/vnd.dialecticore.evidence-pack+json",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.evidence_pack,
            language=episode.source_language,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "adapter": "deterministic_research_scaffold",
                "evidence_pack_id": str(pack.id),
                "schema_version": pack.schema_version,
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
                "evidence_pack": pack.model_dump(mode="json"),
                "source_count": len(pack.source_index),
                "sub_question_count": len(pack.sub_questions),
                "supported_claim_count": len(pack.supported_claims),
                "verified_fact_count": len(pack.verified_facts),
                "uncertain_claim_count": len(pack.uncertain_claims),
                "disputed_claim_count": len(pack.disputed_claims),
                "competing_interpretation_count": len(pack.competing_interpretations),
                "important_statistic_count": len(pack.important_statistics),
                "advanced_extraction_policy": pack.cross_source_summary.get(
                    "advanced_extraction_policy"
                ),
                "causal_scope_policy": pack.cross_source_summary.get("causal_scope_policy"),
                "causal_context_count": pack.cross_source_summary.get(
                    "causal_context_count",
                    0,
                ),
                "scope_qualifier_count": pack.cross_source_summary.get(
                    "scope_qualifier_count",
                    0,
                ),
                "claim_facet_policy": pack.cross_source_summary.get("claim_facet_policy"),
                "claim_facet_count": pack.cross_source_summary.get("claim_facet_count", 0),
                "facet_agreement_count": pack.cross_source_summary.get(
                    "facet_agreement_count",
                    0,
                ),
                "facet_conflict_count": pack.cross_source_summary.get(
                    "facet_conflict_count",
                    0,
                ),
                "claim_support_policy": pack.cross_source_summary.get("claim_support_policy"),
                "claim_support_group_count": pack.cross_source_summary.get(
                    "claim_support_group_count",
                    0,
                ),
                "corroborated_claim_group_count": pack.cross_source_summary.get(
                    "corroborated_claim_group_count",
                    0,
                ),
                "disputed_claim_group_count": pack.cross_source_summary.get(
                    "disputed_claim_group_count",
                    0,
                ),
                "single_source_claim_group_count": pack.cross_source_summary.get(
                    "single_source_claim_group_count",
                    0,
                ),
                "source_agreement_count": len(pack.source_agreements),
                "source_conflict_count": len(pack.source_conflicts),
                "cross_source_cluster_count": pack.cross_source_summary.get("cluster_count", 0),
                "retrieved_source_count": self._retrieved_source_count(pack),
                "discovered_retrieval_target_count": len(discovered_targets),
                "source_ranking_count": len(pack.source_rankings),
                "strong_source_count": pack.source_policy.get("strong_source_count", 0),
                "highest_ranked_source_id": pack.source_policy.get("highest_ranked_source_id"),
                "retrieval_attempt_count": len(retrieval_tool_log),
                "retrieval_success_count": sum(
                    1 for entry in retrieval_tool_log if entry["status"] == "succeeded"
                ),
                "retrieval_failure_count": sum(
                    1 for entry in retrieval_tool_log if entry["status"] != "succeeded"
                ),
                "discovery_attempt_count": len(discovery_tool_log),
                "discovery_success_count": sum(
                    1 for entry in discovery_tool_log if entry["status"] == "succeeded"
                ),
                "discovery_failure_count": sum(
                    1 for entry in discovery_tool_log if entry["status"] != "succeeded"
                ),
                "discovery_tool_log": discovery_tool_log,
                "retrieval_tool_log": retrieval_tool_log,
                "advanced_extraction_tool_log": pack.cross_source_summary.get(
                    "advanced_extraction_tool_log",
                    [],
                ),
                "advanced_extraction_attempt_count": pack.cross_source_summary.get(
                    "advanced_extraction_attempt_count",
                    0,
                ),
                "advanced_extraction_success_count": pack.cross_source_summary.get(
                    "advanced_extraction_success_count",
                    0,
                ),
                "advanced_extraction_accepted_claim_count": pack.cross_source_summary.get(
                    "advanced_extraction_accepted_claim_count",
                    0,
                ),
                "advanced_extraction_invalid_claim_count": pack.cross_source_summary.get(
                    "advanced_extraction_invalid_claim_count",
                    0,
                ),
            },
        )
        episode.assets.append(asset)
        qc = self._evidence_pack_qc(episode, asset, pack)
        episode.quality_results.append(qc)
        approval_required = (
            request.require_approval
            if request.require_approval is not None
            else episode.definition.research.approval_required
        )
        if approval_required:
            self._append_research_approval(episode, request.user_id)
            episode.status = EpisodeStatus.research_review
            self.production_control.record_stage(
                episode,
                EpisodeStatus.research_review,
                "research_service.build_evidence_pack",
            )
        else:
            episode.status = previous_status
            self.production_control.record_stage(
                episode,
                episode.status,
                "research_service.build_evidence_pack",
            )
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="research.evidence_pack.created",
                    actor=request.user_id or "system",
                    details={
                        "evidence_pack_asset_id": str(asset.id),
                        "evidence_pack_id": str(pack.id),
                        "source_count": len(pack.source_index),
                        "discovered_retrieval_target_count": len(discovered_targets),
                        "discovery_attempt_count": len(discovery_tool_log),
                        "discovery_success_count": sum(
                            1 for entry in discovery_tool_log if entry["status"] == "succeeded"
                        ),
                        "retrieval_attempt_count": len(retrieval_tool_log),
                        "retrieval_success_count": sum(
                            1 for entry in retrieval_tool_log if entry["status"] == "succeeded"
                        ),
                        "checksum": stored.checksum,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="research.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "evidence_pack_asset_id": str(asset.id),
                        "status": qc.status,
                        "failure_count": qc.details["failure_count"],
                        "warning_count": qc.details["warning_count"],
                    },
                ),
            ]
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def run_claim_qc(
        self,
        episode: Episode,
        request: ResearchClaimQcRequest,
    ) -> Episode:
        evidence_asset = self._target_evidence_pack_asset(
            episode,
            request.evidence_pack_asset_id,
        )
        pack = self._evidence_pack_json(evidence_asset)
        transcript = self._target_transcript(episode, request.transcript_version_id)
        qc = self._claim_qc(episode, transcript, evidence_asset, pack)
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="research.claim_qc.completed",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "evidence_pack_asset_id": str(evidence_asset.id),
                    "status": qc.status,
                    "failure_count": qc.details["failure_count"],
                    "warning_count": qc.details["warning_count"],
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def review_evidence_source(
        self,
        episode: Episode,
        request: ResearchSourceReviewRequest,
    ) -> Episode:
        evidence_asset = self._target_evidence_pack_asset(
            episode,
            request.evidence_pack_asset_id,
        )
        pack = self._evidence_pack_json(evidence_asset)
        sources = [source for source in pack.get("source_index", []) if isinstance(source, dict)]
        source = next(
            (source for source in sources if source.get("id") == request.source_id),
            None,
        )
        if source is None:
            raise ValueError("evidence source not found")
        reviewed_at = datetime.now(UTC)
        review = {
            "source_id": request.source_id,
            "source_title": source.get("title"),
            "source_type": source.get("source_type"),
            "source_uri": source.get("uri"),
            "content_checksum": source.get("content_checksum"),
            "decision": request.decision,
            "notes": request.notes,
            "reviewer": request.user_id or "system",
            "reviewed_at": reviewed_at.isoformat(),
        }
        reviews = [
            item
            for item in pack.get("source_reviews", [])
            if isinstance(item, dict) and item.get("source_id") != request.source_id
        ]
        reviews.append(review)
        pack["source_reviews"] = sorted(
            reviews,
            key=lambda item: str(item.get("source_id") or ""),
        )
        review_summary = self._source_review_summary(pack)
        pack["source_review_summary"] = review_summary
        evidence_asset.generation_metadata["evidence_pack"] = pack
        evidence_asset.generation_metadata["source_review_policy"] = review_summary["review_policy"]
        evidence_asset.generation_metadata["source_review_count"] = review_summary[
            "reviewed_source_count"
        ]
        evidence_asset.generation_metadata["approved_source_count"] = review_summary[
            "approved_source_count"
        ]
        evidence_asset.generation_metadata["rejected_source_count"] = review_summary[
            "rejected_source_count"
        ]
        evidence_asset.generation_metadata["needs_revision_source_count"] = review_summary[
            "needs_revision_source_count"
        ]
        evidence_asset.generation_metadata["unreviewed_external_source_count"] = review_summary[
            "unreviewed_external_source_count"
        ]
        evidence_asset.updated_at = reviewed_at
        qc = self._source_review_qc(episode, evidence_asset, pack)
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="research.source_review.recorded",
                actor=request.user_id or "system",
                details={
                    "evidence_pack_asset_id": str(evidence_asset.id),
                    "source_id": request.source_id,
                    "decision": request.decision,
                    "status": qc.status,
                    "reviewed_source_count": review_summary["reviewed_source_count"],
                    "unreviewed_external_source_count": review_summary[
                        "unreviewed_external_source_count"
                    ],
                },
            )
        )
        episode.updated_at = reviewed_at
        return episode

    def latest_evidence_pack(self, episode: Episode) -> tuple[Asset, dict] | None:
        asset = self._latest_evidence_pack_asset(episode)
        if asset is None:
            return None
        return asset, self._evidence_pack_json(asset)

    def _build_pack(
        self,
        episode: Episode,
        supplied_sources: list[ResearchSourceInput],
    ) -> EvidencePack:
        topic = episode.definition.topic
        source_index = [
            EvidenceSource(
                id="episode-definition",
                title=f"Episode definition: {episode.title}",
                source_type="episode_configuration",
                uri=f"dialecticore://episodes/{episode.id}/definition",
                confidence=1.0,
                summary="Configuration supplied by the producer for this episode.",
            )
        ]
        for dimension in topic.required_dimensions:
            source_index.append(
                EvidenceSource(
                    id=f"dimension-{self._slug(dimension)}",
                    title=f"Required discussion dimension: {dimension}",
                    source_type="episode_required_dimension",
                    uri=f"dialecticore://episodes/{episode.id}/dimensions/{self._slug(dimension)}",
                    confidence=1.0,
                    summary=(
                        "Producer-provided dimension that the discussion and QC should cover."
                    ),
                )
            )

        source_claims: list[EvidenceClaim] = []
        statistic_claims: list[EvidenceClaim] = []
        verified_facts_from_sources: list[EvidenceClaim] = []
        uncertain_claims_from_sources: list[EvidenceClaim] = []
        disputed_claims: list[EvidenceClaim] = []
        competing_interpretations_from_sources: list[EvidenceClaim] = []
        advanced_extraction_counts: dict[str, int] = {}
        advanced_extraction_tool_log: list[dict[str, object]] = []
        advanced_extraction_accepted_claim_count = 0
        advanced_extraction_invalid_claim_count = 0
        seen_source_keys = {
            self._source_dedupe_key(source.title, source.uri) for source in source_index
        }
        seen_source_ids = {source.id for source in source_index}
        for source in supplied_sources:
            dedupe_key = self._source_dedupe_key(source.title, source.uri)
            if dedupe_key in seen_source_keys:
                continue
            seen_source_keys.add(dedupe_key)
            evidence_source = self._evidence_source_from_input(source)
            evidence_source.id = self._unique_source_id(evidence_source.id, seen_source_ids)
            seen_source_ids.add(evidence_source.id)
            source_index.append(evidence_source)
            extracted = self._claims_from_source(
                source=source,
                source_id=evidence_source.id,
                topic=topic.central_question,
                dimensions=topic.required_dimensions,
            )
            source_claims.extend(extracted["supported"])
            statistic_claims.extend(extracted["statistics"])
            verified_facts_from_sources.extend(extracted["verified_facts"])
            uncertain_claims_from_sources.extend(extracted["uncertain"])
            disputed_claims.extend(extracted["disputed"])
            competing_interpretations_from_sources.extend(extracted["competing_interpretations"])
            for key, count in extracted["advanced_counts"].items():
                advanced_extraction_counts[key] = advanced_extraction_counts.get(key, 0) + count
            advanced_extracted, advanced_log = self._advanced_claims_from_source(
                source=source,
                source_id=evidence_source.id,
                topic=topic.central_question,
                dimensions=topic.required_dimensions,
                extraction_index=len(advanced_extraction_tool_log) + 1,
            )
            if advanced_log is not None:
                advanced_extraction_tool_log.append(advanced_log)
                advanced_extraction_accepted_claim_count += int(
                    advanced_log.get("accepted_claim_count") or 0
                )
                advanced_extraction_invalid_claim_count += int(
                    advanced_log.get("invalid_claim_count") or 0
                )
            for key, count in advanced_extracted["advanced_counts"].items():
                advanced_extraction_counts[key] = advanced_extraction_counts.get(key, 0) + count
            source_claims.extend(advanced_extracted["supported"])
            statistic_claims.extend(advanced_extracted["statistics"])
            verified_facts_from_sources.extend(advanced_extracted["verified_facts"])
            uncertain_claims_from_sources.extend(advanced_extracted["uncertain"])
            disputed_claims.extend(advanced_extracted["disputed"])
            competing_interpretations_from_sources.extend(
                advanced_extracted["competing_interpretations"]
            )

        sub_questions = [
            topic.central_question,
            *[
                f"How does {dimension} affect {topic.central_question}"
                for dimension in topic.required_dimensions
            ],
        ]
        definitions = [
            EvidenceClaim(
                id="central-question",
                text=topic.central_question,
                claim_type="research_question",
                confidence=1.0,
                evidence_refs=["episode-definition"],
            )
        ]
        supported_claims = [
            EvidenceClaim(
                id=f"dimension-{self._slug(dimension)}",
                text=f"The episode must address {dimension}.",
                claim_type="discussion_requirement",
                confidence=1.0,
                evidence_refs=["episode-definition", f"dimension-{self._slug(dimension)}"],
            )
            for dimension in topic.required_dimensions
        ]
        supported_claims.extend(source_claims)
        verified_facts = verified_facts_from_sources
        uncertain_claims = [
            EvidenceClaim(
                id=f"exclusion-{self._slug(exclusion)}",
                text=f"{exclusion} is outside the configured episode scope.",
                claim_type="scope_exclusion",
                confidence=1.0,
                evidence_refs=["episode-definition"],
                uncertainty="Excluded by producer configuration, not by external evidence.",
            )
            for exclusion in topic.exclusions
        ]
        uncertain_claims.extend(uncertain_claims_from_sources)
        source_rankings = self._source_rankings(source_index)
        source_policy = self._source_policy_summary(source_rankings)
        extracted_source_claims = [
            *source_claims,
            *statistic_claims,
            *verified_facts_from_sources,
            *uncertain_claims_from_sources,
            *disputed_claims,
            *competing_interpretations_from_sources,
        ]
        cross_source_analysis = self._cross_source_analysis(extracted_source_claims, source_index)
        claim_facets = self._claim_facets_from_claims(extracted_source_claims)
        causal_scope_contexts = self._causal_scope_contexts_from_claims(extracted_source_claims)
        causal_context_count = sum(
            1 for item in causal_scope_contexts if item.get("context_type") == "causal"
        )
        scope_qualifier_count = sum(
            1 for item in causal_scope_contexts if item.get("context_type") == "scope"
        )
        cross_source_summary = {
            **cross_source_analysis["summary"],
            "advanced_extraction_policy": "deterministic_fact_patterns_v1",
            "advanced_fact_count": len(verified_facts_from_sources),
            "competing_interpretation_count": len(competing_interpretations_from_sources),
            "advanced_extraction_counts": advanced_extraction_counts,
            "causal_scope_policy": "deterministic_causal_scope_context_v1",
            "causal_context_count": causal_context_count,
            "scope_qualifier_count": scope_qualifier_count,
            "causal_scope_contexts": causal_scope_contexts,
            "external_advanced_extraction_policy": (
                "source_bound_external_extractor_v1" if advanced_extraction_tool_log else "disabled"
            ),
            "advanced_extraction_tool_log": advanced_extraction_tool_log,
            "advanced_extraction_attempt_count": len(advanced_extraction_tool_log),
            "advanced_extraction_success_count": sum(
                1 for entry in advanced_extraction_tool_log if entry.get("status") == "succeeded"
            ),
            "advanced_extraction_accepted_claim_count": advanced_extraction_accepted_claim_count,
            "advanced_extraction_invalid_claim_count": advanced_extraction_invalid_claim_count,
            "claim_facet_policy": "deterministic_relation_quantity_facets_v1",
            "claim_facet_count": len(claim_facets),
            "claim_facets": claim_facets,
        }
        return EvidencePack(
            episode_id=episode.id,
            topic=topic,
            research_depth=episode.definition.research.depth,
            sub_questions=sub_questions,
            definitions=definitions,
            verified_facts=verified_facts,
            supported_claims=supported_claims,
            uncertain_claims=uncertain_claims,
            disputed_claims=disputed_claims,
            competing_interpretations=competing_interpretations_from_sources,
            important_statistics=statistic_claims,
            source_index=source_index,
            source_rankings=source_rankings,
            source_policy=source_policy,
            source_agreements=cross_source_analysis["agreements"],
            source_conflicts=cross_source_analysis["conflicts"],
            cross_source_summary=cross_source_summary,
            suggested_discussion_dimensions=topic.required_dimensions,
            fact_check_rules=[
                (
                    "Claims marked verified, supported, statistic, or factual must "
                    "cite source_index ids."
                ),
                "Opinion and prediction claims may be uncited but must remain labelled as such.",
                "Claims with unknown evidence_refs are unsupported until a source is added.",
                (
                    "Retrieved external sources should replace or supplement "
                    "configuration-only sources."
                ),
                "Prefer source_rankings order for factual claims with competing sources.",
                (
                    "Use source_agreements and source_conflicts to identify "
                    "corroborated claims and disputed factual ground."
                ),
            ],
        )

    def _evidence_pack_qc(
        self,
        episode: Episode,
        asset: Asset,
        pack: EvidencePack,
    ) -> QualityResult:
        issues: list[dict] = []
        if not asset.storage_uri:
            issues.append({"severity": "fail", "issue": "evidence_pack_missing_storage"})
        if not asset.checksum:
            issues.append({"severity": "fail", "issue": "evidence_pack_missing_checksum"})
        if not pack.sub_questions:
            issues.append({"severity": "fail", "issue": "evidence_pack_missing_sub_questions"})
        if not pack.source_index:
            issues.append({"severity": "fail", "issue": "evidence_pack_missing_sources"})
        if not pack.fact_check_rules:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "evidence_pack_missing_fact_check_rules",
                }
            )
        if self._retrieved_source_count(pack) == 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "evidence_pack_has_no_retrieved_sources",
                }
            )
        retrieved_rankings = [
            ranking
            for ranking in pack.source_rankings
            if not str(ranking.get("source_type", "")).startswith("episode_")
        ]
        if self._retrieved_source_count(pack) > 0 and not retrieved_rankings:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "evidence_pack_missing_source_rankings",
                }
            )
        policy = pack.source_policy
        if self._retrieved_source_count(pack) >= 2 and policy.get("strong_source_count", 0) == 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "evidence_pack_has_no_strong_external_sources",
                }
            )
        if policy.get("oldest_external_source_age_years", 0) >= 10:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "evidence_pack_contains_stale_external_source",
                    "oldest_external_source_age_years": policy.get(
                        "oldest_external_source_age_years"
                    ),
                }
            )
        if self._retrieved_source_count(pack) >= 2 and not pack.cross_source_summary:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "evidence_pack_missing_cross_source_summary",
                }
            )
        invalid_advanced_claim_count = int(
            pack.cross_source_summary.get("advanced_extraction_invalid_claim_count") or 0
        )
        if invalid_advanced_claim_count:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "advanced_extraction_rejected_untrusted_claims",
                    "invalid_claim_count": invalid_advanced_claim_count,
                }
            )
        source_ids = {source.id for source in pack.source_index}
        for claim in [
            *pack.definitions,
            *pack.verified_facts,
            *pack.supported_claims,
            *pack.uncertain_claims,
            *pack.disputed_claims,
            *pack.competing_interpretations,
            *pack.important_statistics,
        ]:
            missing_refs = [ref for ref in claim.evidence_refs if ref not in source_ids]
            if missing_refs:
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "evidence_claim_unknown_source_ref",
                        "claim_id": claim.id,
                        "missing_refs": missing_refs,
                    }
                )

        return self._quality_result(
            episode=episode,
            target_type="evidence_pack_asset",
            target_id=str(asset.id),
            check_type="evidence_pack_integrity",
            issues=issues,
            details={
                "evidence_pack_asset_id": str(asset.id),
                "evidence_pack_id": str(pack.id),
                "source_count": len(pack.source_index),
                "retrieved_source_count": self._retrieved_source_count(pack),
                "discovered_retrieval_target_count": int(
                    asset.generation_metadata.get("discovered_retrieval_target_count") or 0
                ),
                "discovery_attempt_count": int(
                    asset.generation_metadata.get("discovery_attempt_count") or 0
                ),
                "discovery_success_count": int(
                    asset.generation_metadata.get("discovery_success_count") or 0
                ),
                "discovery_failure_count": int(
                    asset.generation_metadata.get("discovery_failure_count") or 0
                ),
                "sub_question_count": len(pack.sub_questions),
                "supported_claim_count": len(pack.supported_claims),
                "verified_fact_count": len(pack.verified_facts),
                "uncertain_claim_count": len(pack.uncertain_claims),
                "disputed_claim_count": len(pack.disputed_claims),
                "competing_interpretation_count": len(pack.competing_interpretations),
                "important_statistic_count": len(pack.important_statistics),
                "advanced_extraction_policy": pack.cross_source_summary.get(
                    "advanced_extraction_policy"
                ),
                "advanced_extraction_counts": pack.cross_source_summary.get(
                    "advanced_extraction_counts",
                    {},
                ),
                "causal_scope_policy": pack.cross_source_summary.get("causal_scope_policy"),
                "causal_context_count": pack.cross_source_summary.get(
                    "causal_context_count",
                    0,
                ),
                "scope_qualifier_count": pack.cross_source_summary.get(
                    "scope_qualifier_count",
                    0,
                ),
                "external_advanced_extraction_policy": pack.cross_source_summary.get(
                    "external_advanced_extraction_policy"
                ),
                "advanced_extraction_attempt_count": pack.cross_source_summary.get(
                    "advanced_extraction_attempt_count",
                    0,
                ),
                "advanced_extraction_success_count": pack.cross_source_summary.get(
                    "advanced_extraction_success_count",
                    0,
                ),
                "advanced_extraction_accepted_claim_count": pack.cross_source_summary.get(
                    "advanced_extraction_accepted_claim_count",
                    0,
                ),
                "advanced_extraction_invalid_claim_count": pack.cross_source_summary.get(
                    "advanced_extraction_invalid_claim_count",
                    0,
                ),
                "claim_facet_policy": pack.cross_source_summary.get("claim_facet_policy"),
                "claim_facet_count": pack.cross_source_summary.get("claim_facet_count", 0),
                "facet_agreement_count": pack.cross_source_summary.get(
                    "facet_agreement_count",
                    0,
                ),
                "facet_conflict_count": pack.cross_source_summary.get(
                    "facet_conflict_count",
                    0,
                ),
                "claim_support_policy": pack.cross_source_summary.get("claim_support_policy"),
                "claim_support_group_count": pack.cross_source_summary.get(
                    "claim_support_group_count",
                    0,
                ),
                "corroborated_claim_group_count": pack.cross_source_summary.get(
                    "corroborated_claim_group_count",
                    0,
                ),
                "disputed_claim_group_count": pack.cross_source_summary.get(
                    "disputed_claim_group_count",
                    0,
                ),
                "single_source_claim_group_count": pack.cross_source_summary.get(
                    "single_source_claim_group_count",
                    0,
                ),
                "source_agreement_count": len(pack.source_agreements),
                "source_conflict_count": len(pack.source_conflicts),
                "cross_source_cluster_count": pack.cross_source_summary.get(
                    "cluster_count",
                    0,
                ),
                "source_ranking_count": len(pack.source_rankings),
                "strong_source_count": pack.source_policy.get("strong_source_count", 0),
                "highest_ranked_source_id": pack.source_policy.get("highest_ranked_source_id"),
                "suggested_dimension_count": len(pack.suggested_discussion_dimensions),
            },
        )

    def _claim_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        evidence_asset: Asset,
        pack: dict,
    ) -> QualityResult:
        issues: list[dict] = []
        source_ids = {
            source["id"]
            for source in pack.get("source_index", [])
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        }
        claim_count = 0
        cited_claim_count = 0
        unsupported_claim_count = 0
        opinion_or_prediction_count = 0
        invalid_ref_count = 0
        source_required = episode.definition.research.require_source_links
        for turn in transcript.turns:
            for claim in turn.claims:
                claim_count += 1
                invalid_refs = [ref for ref in claim.evidence_refs if ref not in source_ids]
                if invalid_refs:
                    invalid_ref_count += len(invalid_refs)
                    issues.append(
                        {
                            "severity": "fail",
                            "issue": "claim_unknown_evidence_ref",
                            "transcript_turn_id": str(turn.id),
                            "claim": claim.text,
                            "invalid_refs": invalid_refs,
                        }
                    )
                if claim.evidence_refs:
                    cited_claim_count += 1
                    continue
                if claim.claim_type in {"opinion", "prediction"}:
                    opinion_or_prediction_count += 1
                    issues.append(
                        {
                            "severity": "warning",
                            "issue": "uncited_opinion_or_prediction",
                            "transcript_turn_id": str(turn.id),
                            "claim": claim.text,
                            "claim_type": claim.claim_type,
                        }
                    )
                    continue
                unsupported_claim_count += 1
                severity = "fail" if source_required else "warning"
                if episode.definition.quality.block_on_unsupported_high_impact_claims:
                    severity = "fail"
                issues.append(
                    {
                        "severity": severity,
                        "issue": "unsupported_claim",
                        "transcript_turn_id": str(turn.id),
                        "claim": claim.text,
                        "claim_type": claim.claim_type,
                    }
                )

        return self._quality_result(
            episode=episode,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="claim_citation_integrity",
            issues=issues,
            details={
                "transcript_version_id": str(transcript.id),
                "evidence_pack_asset_id": str(evidence_asset.id),
                "claim_count": claim_count,
                "cited_claim_count": cited_claim_count,
                "unsupported_claim_count": unsupported_claim_count,
                "opinion_or_prediction_count": opinion_or_prediction_count,
                "invalid_evidence_ref_count": invalid_ref_count,
                "source_count": len(source_ids),
                "source_links_required": source_required,
            },
        )

    def _source_review_qc(
        self,
        episode: Episode,
        evidence_asset: Asset,
        pack: dict,
    ) -> QualityResult:
        summary = self._source_review_summary(pack)
        issues: list[dict] = []
        if summary["unreviewed_external_source_count"] > 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "external_sources_pending_human_review",
                    "unreviewed_external_source_count": summary["unreviewed_external_source_count"],
                }
            )
        for source_id in summary["rejected_source_ids"]:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "evidence_source_rejected_by_reviewer",
                    "source_id": source_id,
                }
            )
        for source_id in summary["needs_revision_source_ids"]:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "evidence_source_needs_revision",
                    "source_id": source_id,
                }
            )
        return self._quality_result(
            episode=episode,
            target_type="evidence_pack_asset",
            target_id=str(evidence_asset.id),
            check_type="research_source_review_integrity",
            issues=issues,
            details={
                "evidence_pack_asset_id": str(evidence_asset.id),
                **summary,
            },
        )

    def _source_review_summary(self, pack: dict) -> dict[str, object]:
        sources = [
            source
            for source in pack.get("source_index", [])
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        ]
        external_source_ids = {
            source["id"]
            for source in sources
            if not str(source.get("source_type") or "").startswith("episode_")
        }
        reviews = [
            review
            for review in pack.get("source_reviews", [])
            if isinstance(review, dict) and isinstance(review.get("source_id"), str)
        ]
        latest_by_source = {str(review["source_id"]): review for review in reviews}
        approved_source_ids = sorted(
            source_id
            for source_id, review in latest_by_source.items()
            if review.get("decision") == "approved"
        )
        rejected_source_ids = sorted(
            source_id
            for source_id, review in latest_by_source.items()
            if review.get("decision") == "rejected"
        )
        needs_revision_source_ids = sorted(
            source_id
            for source_id, review in latest_by_source.items()
            if review.get("decision") == "needs_revision"
        )
        reviewed_external_source_ids = sorted(set(latest_by_source) & external_source_ids)
        unreviewed_external_source_ids = sorted(external_source_ids - set(latest_by_source))
        return {
            "review_policy": "human_source_review_v1",
            "source_count": len(sources),
            "external_source_count": len(external_source_ids),
            "reviewed_source_count": len(latest_by_source),
            "reviewed_external_source_count": len(reviewed_external_source_ids),
            "unreviewed_external_source_count": len(unreviewed_external_source_ids),
            "approved_source_count": len(approved_source_ids),
            "rejected_source_count": len(rejected_source_ids),
            "needs_revision_source_count": len(needs_revision_source_ids),
            "approved_source_ids": approved_source_ids,
            "rejected_source_ids": rejected_source_ids,
            "needs_revision_source_ids": needs_revision_source_ids,
            "reviewed_external_source_ids": reviewed_external_source_ids,
            "unreviewed_external_source_ids": unreviewed_external_source_ids,
        }

    def _quality_result(
        self,
        episode: Episode,
        target_type: str,
        target_id: str,
        check_type: str,
        issues: list[dict],
        details: dict,
    ) -> QualityResult:
        failure_count = sum(1 for issue in issues if issue["severity"] == "fail")
        warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
        if failure_count:
            severity = QualitySeverity.fail
        elif warning_count:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_
        return QualityResult(
            episode_id=episode.id,
            target_type=target_type,
            target_id=target_id,
            check_type=check_type,
            severity=severity,
            status=severity.value,
            score=0.0 if failure_count else max(0.0, 1.0 - (0.04 * warning_count)),
            details={
                **details,
                "issue_count": len(issues),
                "failure_count": failure_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _append_research_approval(self, episode: Episode, user_id: str | None) -> None:
        if any(
            approval.stage == "research_review" and approval.decision == "pending"
            for approval in episode.approvals
        ):
            return
        episode.approvals.append(
            Approval(
                episode_id=episode.id,
                stage="research_review",
                decision="pending",
                comment="Research evidence pack requires review before discussion.",
                user_id=user_id,
            )
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="approval.required",
                actor=user_id or "system",
                details={"stage": "research_review"},
            )
        )

    def _target_transcript(
        self,
        episode: Episode,
        transcript_version_id: UUID | None,
    ) -> TranscriptVersion:
        if transcript_version_id is not None:
            transcript = next(
                (item for item in episode.transcripts if item.id == transcript_version_id),
                None,
            )
            if transcript is None:
                raise ValueError("transcript version not found")
            return transcript
        if episode.canonical_transcript_version_id is None:
            raise ValueError("episode has no canonical transcript")
        return (
            next(
                (
                    item
                    for item in episode.transcripts
                    if item.id == episode.canonical_transcript_version_id
                ),
                None,
            )
            or self._raise_transcript_missing()
        )

    def _raise_transcript_missing(self) -> TranscriptVersion:
        raise ValueError("transcript version not found")

    def _target_evidence_pack_asset(
        self,
        episode: Episode,
        evidence_pack_asset_id: UUID | None,
    ) -> Asset:
        if evidence_pack_asset_id is not None:
            asset = next(
                (item for item in episode.assets if item.id == evidence_pack_asset_id),
                None,
            )
            if asset is None or asset.asset_type != AssetType.evidence_pack:
                raise ValueError("evidence pack asset not found")
            if asset.status != "completed":
                raise ValueError("evidence pack asset is not completed")
            return asset
        return self._latest_evidence_pack_asset(episode) or self._raise_evidence_missing()

    def _latest_evidence_pack_asset(self, episode: Episode) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
            ),
            None,
        )

    def _raise_evidence_missing(self) -> Asset:
        raise ValueError("evidence pack not found")

    def _evidence_pack_json(self, asset: Asset) -> dict:
        metadata_pack = asset.generation_metadata.get("evidence_pack")
        if isinstance(metadata_pack, dict):
            return metadata_pack
        if asset.storage_uri is None:
            raise ValueError("evidence pack asset has no storage URI")
        path = self.object_store.path_for_uri(asset.storage_uri)
        if path is None or not path.exists():
            raise ValueError("evidence pack object not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("evidence pack object is not a JSON object")
        return payload

    def _retrieved_source_count(self, pack: EvidencePack) -> int:
        return sum(
            1 for source in pack.source_index if not source.source_type.startswith("episode_")
        )

    def _source_rankings(self, sources: list[EvidenceSource]) -> list[dict[str, object]]:
        rankings = []
        for source in sources:
            source_type = source.source_type.lower()
            is_external = not source_type.startswith("episode_")
            authority_score = self._source_authority_score(source)
            recency_score = self._source_recency_score(source)
            evidence_quality_score = round(
                min(
                    1.0,
                    (source.confidence * 0.58)
                    + (authority_score * 0.24)
                    + (recency_score * 0.12)
                    + ((0.06 if source.content_checksum else 0.0) if is_external else 0.0),
                ),
                3,
            )
            ranking_basis = [
                f"confidence={source.confidence:.3f}",
                f"authority={authority_score:.2f}",
                f"recency={recency_score:.2f}",
            ]
            if source.content_checksum:
                ranking_basis.append("content_checksum_present")
            rankings.append(
                {
                    "source_id": source.id,
                    "title": source.title,
                    "source_type": source.source_type,
                    "uri": source.uri,
                    "confidence": source.confidence,
                    "authority_score": authority_score,
                    "recency_score": recency_score,
                    "evidence_quality_score": evidence_quality_score,
                    "published_year": self._published_year(source.published_at),
                    "content_checksum": source.content_checksum,
                    "external": is_external,
                    "ranking_basis": ranking_basis,
                }
            )
        ranked = sorted(
            rankings,
            key=lambda item: (
                float(item["evidence_quality_score"]),
                float(item["authority_score"]),
                float(item["confidence"]),
                str(item["source_id"]),
            ),
            reverse=True,
        )
        return [
            {
                **item,
                "rank": index,
                "tier": self._source_tier(float(item["evidence_quality_score"])),
            }
            for index, item in enumerate(ranked, start=1)
        ]

    def _source_policy_summary(self, rankings: list[dict[str, object]]) -> dict[str, object]:
        external = [item for item in rankings if item.get("external") is True]
        strong = [
            item for item in external if float(item.get("evidence_quality_score", 0.0)) >= 0.75
        ]
        published_years = [
            int(item["published_year"])
            for item in external
            if isinstance(item.get("published_year"), int)
        ]
        current_year = datetime.now(UTC).year
        return {
            "ranking_policy": "confidence_authority_recency_checksum_v1",
            "minimum_strong_external_sources": 1,
            "external_source_count": len(external),
            "strong_source_count": len(strong),
            "highest_ranked_source_id": rankings[0]["source_id"] if rankings else None,
            "highest_ranked_external_source_id": external[0]["source_id"] if external else None,
            "oldest_external_source_age_years": (
                current_year - min(published_years) if published_years else 0
            ),
            "source_diversity": sorted(
                {str(item.get("source_type")) for item in external if item.get("source_type")}
            ),
        }

    def _source_authority_score(self, source: EvidenceSource) -> float:
        source_type = source.source_type.lower()
        if source_type in {"government_report", "standards_body", "academic_paper"}:
            score = 0.9
        elif source_type in {"industry_report", "official_documentation"}:
            score = 0.78
        elif source_type in {"news_article", "web_page"}:
            score = 0.55
        elif source_type == "manual_source":
            score = 0.5
        elif source_type.startswith("episode_"):
            score = 0.35
        else:
            score = 0.45
        host = urlparse(source.uri or "").netloc.lower()
        if host.endswith(".gov") or host.endswith(".edu"):
            score += 0.08
        return round(min(1.0, score), 3)

    def _source_recency_score(self, source: EvidenceSource) -> float:
        year = self._published_year(source.published_at)
        if year is None:
            return 0.45 if not source.source_type.startswith("episode_") else 0.25
        age = max(0, datetime.now(UTC).year - year)
        return round(max(0.0, 1.0 - (age * 0.08)), 3)

    def _source_tier(self, score: float) -> str:
        if score >= 0.85:
            return "primary"
        if score >= 0.75:
            return "strong"
        if score >= 0.6:
            return "supporting"
        return "context"

    def _retrieve_sources(
        self,
        targets: list[ResearchRetrievalTarget],
    ) -> tuple[list[ResearchSourceInput], list[dict[str, object]]]:
        sources: list[ResearchSourceInput] = []
        tool_log: list[dict[str, object]] = []
        for target in targets:
            started_at = datetime.now(UTC)
            start = time.monotonic()
            log_entry: dict[str, object] = {
                "tool": "http_get",
                "uri": target.uri,
                "source_type": target.source_type,
                "started_at": started_at.isoformat(),
            }
            if target.discovered_by:
                log_entry.update(
                    {
                        "discovered_by": target.discovered_by,
                        "discovery_query": target.discovery_query,
                        "discovery_rank": target.discovery_rank,
                    }
                )
            parsed = urlparse(target.uri)
            if parsed.scheme.lower() not in {"http", "https"}:
                log_entry.update(
                    {
                        "status": "skipped",
                        "error": "unsupported_uri_scheme",
                        "completed_at": datetime.now(UTC).isoformat(),
                        "elapsed_ms": int((time.monotonic() - start) * 1000),
                    }
                )
                tool_log.append(log_entry)
                continue
            try:
                fetched = self._fetch_retrieval_target(target.uri)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                log_entry.update(
                    {
                        "status": "failed",
                        "error": type(exc).__name__,
                        "message": str(exc)[:240],
                        "completed_at": datetime.now(UTC).isoformat(),
                        "elapsed_ms": int((time.monotonic() - start) * 1000),
                    }
                )
                tool_log.append(log_entry)
                continue
            text = self._text_from_retrieved_payload(
                fetched["payload"],
                str(fetched.get("content_type") or ""),
            )
            checksum = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            log_entry.update(
                {
                    "status": "succeeded",
                    "http_status": fetched["http_status"],
                    "content_type": fetched.get("content_type"),
                    "byte_count": fetched["byte_count"],
                    "content_checksum": checksum,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                }
            )
            tool_log.append(log_entry)
            sources.append(
                ResearchSourceInput(
                    title=target.title or self._title_from_uri(target.uri),
                    uri=target.uri,
                    source_type=target.source_type,
                    author=target.author,
                    published_at=target.published_at,
                    retrieved_at=datetime.now(UTC),
                    confidence=target.confidence,
                    content=text,
                    summary=target.summary,
                )
            )
        return sources, tool_log

    def _discover_retrieval_targets(
        self,
        episode: Episode,
        request: ResearchBuildRequest,
    ) -> tuple[list[ResearchRetrievalTarget], list[dict[str, object]]]:
        if not request.discover_sources:
            return [], []
        queries = self._discovery_queries(episode, request.discovery_queries)
        template = getattr(self.settings, "research_discovery_url_template", None)
        if not getattr(self.settings, "research_discovery_enabled", False) or not template:
            return [], [
                {
                    "tool": "search_discovery",
                    "status": "skipped",
                    "error": "discovery_not_configured",
                    "query_count": len(queries),
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            ]
        targets: list[ResearchRetrievalTarget] = []
        tool_log: list[dict[str, object]] = []
        seen_uris: set[str] = set()
        for query in queries:
            started_at = datetime.now(UTC)
            start = time.monotonic()
            discovery_uri = self._discovery_uri(template, query)
            log_entry: dict[str, object] = {
                "tool": "search_discovery",
                "query": query,
                "uri": discovery_uri,
                "started_at": started_at.isoformat(),
            }
            try:
                fetched = self._fetch_retrieval_target(discovery_uri)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                log_entry.update(
                    {
                        "status": "failed",
                        "error": type(exc).__name__,
                        "message": str(exc)[:240],
                        "completed_at": datetime.now(UTC).isoformat(),
                        "elapsed_ms": int((time.monotonic() - start) * 1000),
                    }
                )
                tool_log.append(log_entry)
                continue
            payload_text = self._text_from_retrieved_payload(
                fetched["payload"],
                str(fetched.get("content_type") or ""),
            )
            discovered = self._targets_from_discovery_payload(
                payload_text,
                str(fetched.get("content_type") or ""),
                query,
            )
            selected: list[ResearchRetrievalTarget] = []
            for target in discovered:
                uri_key = self._normalized_uri(target.uri)
                if uri_key in seen_uris:
                    continue
                seen_uris.add(uri_key)
                selected.append(target)
                targets.append(target)
            checksum = "sha256:" + hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
            log_entry.update(
                {
                    "status": "succeeded",
                    "http_status": fetched["http_status"],
                    "content_type": fetched.get("content_type"),
                    "byte_count": fetched["byte_count"],
                    "content_checksum": checksum,
                    "discovered_count": len(discovered),
                    "selected_count": len(selected),
                    "completed_at": datetime.now(UTC).isoformat(),
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                }
            )
            tool_log.append(log_entry)
        return targets, tool_log

    def _discovery_queries(
        self,
        episode: Episode,
        requested_queries: list[str],
    ) -> list[str]:
        query_limit = getattr(self.settings, "research_discovery_max_queries", 4)
        raw_queries = requested_queries or [
            episode.definition.topic.central_question,
            *[
                f"{episode.definition.topic.central_question} {dimension}"
                for dimension in episode.definition.topic.required_dimensions
            ],
        ]
        queries: list[str] = []
        seen: set[str] = set()
        for query in raw_queries:
            normalized = " ".join(str(query).split())
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            queries.append(normalized)
            if len(queries) >= query_limit:
                break
        return queries

    def _discovery_uri(self, template: str, query: str) -> str:
        encoded = quote_plus(query)
        if "{query}" in template:
            return template.replace("{query}", encoded)
        separator = "&" if "?" in template else "?"
        return f"{template}{separator}q={encoded}"

    def _targets_from_discovery_payload(
        self,
        payload_text: str,
        content_type: str,
        query: str,
    ) -> list[ResearchRetrievalTarget]:
        max_results = getattr(self.settings, "research_discovery_max_results_per_query", 5)
        records = (
            self._json_discovery_records(payload_text)
            if "json" in content_type.lower() or payload_text.strip().startswith(("{", "["))
            else []
        )
        if not records:
            records = self._html_discovery_records(payload_text)
        targets: list[ResearchRetrievalTarget] = []
        for index, record in enumerate(records, start=1):
            uri = str(record.get("uri") or "").strip()
            if urlparse(uri).scheme.lower() not in {"http", "https"}:
                continue
            source_type = str(record.get("source_type") or "").strip()
            targets.append(
                ResearchRetrievalTarget(
                    title=str(record.get("title") or self._title_from_uri(uri)),
                    uri=uri,
                    source_type=source_type or self._source_type_for_uri(uri),
                    published_at=(
                        str(record["published_at"]) if record.get("published_at") else None
                    ),
                    confidence=(
                        float(record["confidence"])
                        if isinstance(record.get("confidence"), int | float)
                        else None
                    ),
                    summary=str(record.get("summary") or ""),
                    discovered_by="search_discovery",
                    discovery_query=query,
                    discovery_rank=index,
                )
            )
            if len(targets) >= max_results:
                break
        return targets

    @staticmethod
    def _source_type_for_uri(uri: str) -> str:
        """Conservatively identify public primary sources returned by discovery."""
        host = (urlparse(uri).hostname or "").lower().rstrip(".")
        government_hosts = (
            host.endswith(".gov")
            or host.endswith(".gov.uk")
            or host.endswith(".europa.eu")
            or host
            in {
                "bundesregierung.de",
                "www.bundesregierung.de",
                "bundesnetzagentur.de",
                "www.bundesnetzagentur.de",
                "bmds.bund.de",
                "www.bmds.bund.de",
                "bmwk.de",
                "www.bmwk.de",
                "destatis.de",
                "www.destatis.de",
                "umweltbundesamt.de",
                "www.umweltbundesamt.de",
            }
        )
        if government_hosts:
            return "government_report"
        if host == "iea.org" or host.endswith(".iea.org"):
            return "industry_report"
        return "web_page"

    def _json_discovery_records(self, payload_text: str) -> list[dict[str, object]]:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        candidates: object
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = (
                payload.get("results")
                or payload.get("items")
                or payload.get("organic_results")
                or (
                    payload.get("webPages", {}) if isinstance(payload.get("webPages"), dict) else {}
                ).get("value")
                or []
            )
        else:
            candidates = []
        if not isinstance(candidates, list):
            return []
        records: list[dict[str, object]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            uri = item.get("url") or item.get("uri") or item.get("link")
            records.append(
                {
                    "uri": uri,
                    "title": item.get("title") or item.get("name"),
                    "summary": item.get("summary")
                    or item.get("snippet")
                    or item.get("description"),
                    "source_type": item.get("source_type") or item.get("sourceType"),
                    "published_at": item.get("published_at") or item.get("datePublished"),
                    "confidence": item.get("confidence"),
                }
            )
        return records

    def _html_discovery_records(self, payload_text: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for match in re.finditer(
            r"<a\b[^>]*href=[\"'](?P<uri>https?://[^\"'#]+)[\"'][^>]*>(?P<title>.*?)</a>",
            payload_text,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            title = unescape(re.sub(r"<[^>]+>", " ", match.group("title")))
            records.append(
                {
                    "uri": match.group("uri"),
                    "title": " ".join(title.split()) or self._title_from_uri(match.group("uri")),
                }
            )
        return records

    def _dedupe_retrieval_targets(
        self,
        targets: list[ResearchRetrievalTarget],
    ) -> list[ResearchRetrievalTarget]:
        unique: list[ResearchRetrievalTarget] = []
        seen: set[str] = set()
        for target in targets:
            key = self._normalized_uri(target.uri)
            if key in seen:
                continue
            seen.add(key)
            unique.append(target)
        return unique

    def _fetch_retrieval_target(self, uri: str) -> dict[str, object]:
        request = Request(
            uri,
            headers={
                "User-Agent": "DialectiCoreResearchBot/0.1",
                "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.2",
            },
        )
        timeout = getattr(self.settings, "research_retrieval_timeout_seconds", 8)
        max_bytes = getattr(self.settings, "research_retrieval_max_bytes", 1_000_000)
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                payload = payload[:max_bytes]
            return {
                "http_status": response.status,
                "content_type": response.headers.get("content-type"),
                "byte_count": len(payload),
                "payload": payload,
            }

    def _post_advanced_extraction_request(
        self,
        uri: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        request_payload = json.dumps(payload).encode("utf-8")
        request = Request(
            uri,
            data=request_payload,
            headers={
                "User-Agent": "DialectiCoreResearchBot/0.1",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = getattr(self.settings, "research_advanced_extraction_timeout_seconds", 15)
        max_bytes = getattr(self.settings, "research_retrieval_max_bytes", 1_000_000)
        with urlopen(request, timeout=timeout) as response:
            response_payload = response.read(max_bytes + 1)
            if len(response_payload) > max_bytes:
                response_payload = response_payload[:max_bytes]
            decoded = self._text_from_retrieved_payload(
                response_payload,
                response.headers.get("content-type") or "application/json",
            )
            parsed = json.loads(decoded)
            if not isinstance(parsed, dict):
                raise ValueError("advanced extraction response is not a JSON object")
            return {
                **parsed,
                "http_status": response.status,
                "content_type": response.headers.get("content-type"),
                "byte_count": len(response_payload),
            }

    def _text_from_retrieved_payload(self, payload: object, content_type: str) -> str:
        if not isinstance(payload, bytes):
            raise ValueError("retrieved payload is not bytes")
        charset_match = re.search(r"charset=([\w.-]+)", content_type, flags=re.IGNORECASE)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if "html" in content_type.lower() or re.search(
            r"<(html|body|p|article|main|section|div)\b", decoded, flags=re.IGNORECASE
        ):
            decoded = re.sub(
                r"<(script|style|noscript)\b[^>]*>.*?</\1>",
                " ",
                decoded,
                flags=re.IGNORECASE | re.DOTALL,
            )
            decoded = re.sub(r"<[^>]+>", " ", decoded)
            decoded = unescape(decoded)
        return " ".join(decoded.split())[:20000]

    def _title_from_uri(self, uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.netloc:
            return f"Retrieved source: {parsed.netloc}{parsed.path}"
        return "Retrieved source"

    def _evidence_source_from_input(self, source: ResearchSourceInput) -> EvidenceSource:
        source_id = source.id or self._slug(source.uri or source.title)
        confidence, score_factors = self._source_score(source)
        content_checksum = (
            "sha256:" + hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            if source.content
            else None
        )
        return EvidenceSource(
            id=source_id,
            title=source.title,
            source_type=source.source_type,
            uri=source.uri,
            author=source.author,
            published_at=source.published_at,
            retrieved_at=source.retrieved_at or datetime.now(UTC),
            confidence=confidence,
            content_checksum=content_checksum,
            score_factors=score_factors,
            summary=source.summary or self._source_summary(source.content),
        )

    def _cross_source_analysis(
        self,
        claims: list[EvidenceClaim],
        sources: list[EvidenceSource],
    ) -> dict[str, object]:
        source_by_id = {source.id: source for source in sources}
        claim_records = [
            self._claim_analysis_record(claim, source_by_id)
            for claim in claims
            if claim.evidence_refs
            and any(
                self._is_external_source(source_by_id.get(source_id))
                for source_id in claim.evidence_refs
            )
        ]
        agreements: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []
        seen_agreements: set[tuple[str, str]] = set()
        seen_conflicts: set[tuple[str, str]] = set()
        facet_agreement_count = 0
        facet_conflict_count = 0
        for left_index, left in enumerate(claim_records):
            for right in claim_records[left_index + 1 :]:
                if not self._different_sources(left, right):
                    continue
                key = tuple(sorted([str(left["claim_id"]), str(right["claim_id"])]))
                facet_relationship = self._facet_claim_relationship(left, right)
                if facet_relationship is not None:
                    entry = self._source_relationship_entry(
                        relationship=str(facet_relationship["relationship"]),
                        left=left,
                        right=right,
                        shared_terms=list(facet_relationship["shared_terms"]),
                        basis=str(facet_relationship["basis"]),
                        relationship_basis="claim_facet",
                        facet_match=facet_relationship["facet_match"],
                    )
                    if facet_relationship["relationship"] == "agreement":
                        if key not in seen_agreements:
                            seen_agreements.add(key)
                            agreements.append(entry)
                            facet_agreement_count += 1
                    elif key not in seen_conflicts:
                        seen_conflicts.add(key)
                        conflicts.append(entry)
                        facet_conflict_count += 1
                    continue
                shared_terms = sorted(set(left["terms"]) & set(right["terms"]))
                if len(shared_terms) < 2:
                    continue
                relationship = self._claim_relationship(left["stance"], right["stance"])
                if relationship == "agreement":
                    if key in seen_agreements:
                        continue
                    seen_agreements.add(key)
                    agreements.append(
                        self._source_relationship_entry(
                            relationship="agreement",
                            left=left,
                            right=right,
                            shared_terms=shared_terms,
                            basis=(
                                "Matched distinct external-source claims with shared topical "
                                "terms and deterministic stance signals."
                            ),
                            relationship_basis="shared_terms_stance",
                        )
                    )
                elif relationship == "conflict":
                    if key in seen_conflicts:
                        continue
                    seen_conflicts.add(key)
                    conflicts.append(
                        self._source_relationship_entry(
                            relationship="conflict",
                            left=left,
                            right=right,
                            shared_terms=shared_terms,
                            basis=(
                                "Matched distinct external-source claims with shared topical "
                                "terms and deterministic stance signals."
                            ),
                            relationship_basis="shared_terms_stance",
                        )
                    )
        ranked_agreements = sorted(
            agreements,
            key=lambda item: (
                float(item["relationship_score"]),
                len(item["shared_terms"]),
                str(item["claim_ids"][0]),
            ),
            reverse=True,
        )[:12]
        ranked_conflicts = sorted(
            conflicts,
            key=lambda item: (
                float(item["relationship_score"]),
                len(item["shared_terms"]),
                str(item["claim_ids"][0]),
            ),
            reverse=True,
        )[:12]
        clusters = {
            term
            for item in [*ranked_agreements, *ranked_conflicts]
            for term in item["shared_terms"]
        }
        claim_support_groups = self._claim_support_groups(claim_records)
        corroborated_claim_group_count = sum(
            1 for group in claim_support_groups if group.get("relationship_state") == "corroborated"
        )
        disputed_claim_group_count = sum(
            1 for group in claim_support_groups if group.get("relationship_state") == "disputed"
        )
        single_source_claim_group_count = sum(
            1
            for group in claim_support_groups
            if group.get("relationship_state") == "single_source"
        )
        summary = {
            "analysis_policy": "deterministic_shared_terms_stance_v1",
            "claim_record_count": len(claim_records),
            "cluster_count": len(clusters),
            "agreement_count": len(ranked_agreements),
            "conflict_count": len(ranked_conflicts),
            "facet_analysis_policy": "deterministic_claim_facet_relationships_v1",
            "facet_agreement_count": facet_agreement_count,
            "facet_conflict_count": facet_conflict_count,
            "claim_support_policy": "deterministic_claim_support_groups_v1",
            "claim_support_group_count": len(claim_support_groups),
            "corroborated_claim_group_count": corroborated_claim_group_count,
            "disputed_claim_group_count": disputed_claim_group_count,
            "single_source_claim_group_count": single_source_claim_group_count,
            "claim_support_groups": claim_support_groups,
            "agreement_source_ids": sorted(
                {source_id for item in ranked_agreements for source_id in item["source_ids"]}
            ),
            "conflict_source_ids": sorted(
                {source_id for item in ranked_conflicts for source_id in item["source_ids"]}
            ),
            "relationship_terms": sorted(clusters),
        }
        return {
            "agreements": ranked_agreements,
            "conflicts": ranked_conflicts,
            "summary": summary,
        }

    def _claim_support_groups(
        self,
        claim_records: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        grouped: dict[tuple[str, ...], list[dict[str, object]]] = {}
        for record in claim_records:
            key = self._claim_support_key(record)
            if key is None:
                continue
            grouped.setdefault(key, []).append(record)
        support_groups: list[dict[str, object]] = []
        for index, (key, records) in enumerate(
            sorted(grouped.items(), key=lambda item: item[0]),
            start=1,
        ):
            source_ids = sorted(
                {
                    source_id
                    for record in records
                    for source_id in record["source_ids"]
                    if isinstance(source_id, str)
                }
            )
            stances = [
                str(record["stance"])
                for record in records
                if record.get("stance") in {"positive", "negative", "uncertain", "contrast"}
            ]
            positive_count = stances.count("positive")
            negative_count = stances.count("negative")
            uncertain_count = stances.count("uncertain")
            relationship_state = self._claim_support_state(
                source_count=len(source_ids),
                positive_count=positive_count,
                negative_count=negative_count,
                uncertain_count=uncertain_count,
            )
            support_groups.append(
                {
                    "id": f"claim-support-{index}",
                    "group_key": "|".join(key),
                    "basis": key[0],
                    "relation": key[1] if key[0] == "facet" else None,
                    "topic_terms": sorted(
                        {
                            term
                            for record in records
                            for term in record["terms"]
                            if isinstance(term, str)
                        }
                    )[:8],
                    "claim_ids": [record["claim_id"] for record in records],
                    "source_ids": source_ids,
                    "source_count": len(source_ids),
                    "claim_count": len(records),
                    "relationship_state": relationship_state,
                    "stance_counts": {
                        "positive": positive_count,
                        "negative": negative_count,
                        "uncertain": uncertain_count,
                        "contrast": stances.count("contrast"),
                    },
                    "representative_claim": records[0]["text"],
                    "evidence_strength": self._claim_support_strength(
                        relationship_state=relationship_state,
                        source_count=len(source_ids),
                    ),
                }
            )
        return sorted(
            support_groups,
            key=lambda group: (
                int(group["source_count"]),
                int(group["claim_count"]),
                str(group["group_key"]),
            ),
            reverse=True,
        )[:16]

    def _claim_support_key(self, record: dict[str, object]) -> tuple[str, ...] | None:
        facet = record.get("facet")
        if isinstance(facet, dict):
            relation = str(facet.get("relation") or "")
            subject_terms = self._facet_topic_terms(str(facet.get("subject") or ""))
            object_terms = self._facet_topic_terms(str(facet.get("object") or ""))
            if relation and subject_terms and object_terms:
                return (
                    "facet",
                    relation,
                    ",".join(subject_terms[:4]),
                    ",".join(object_terms[:4]),
                )
        terms = [term for term in record.get("terms", []) if isinstance(term, str)]
        if len(terms) >= 2:
            return ("terms", *terms[:4])
        return None

    def _claim_support_state(
        self,
        source_count: int,
        positive_count: int,
        negative_count: int,
        uncertain_count: int,
    ) -> str:
        if positive_count and negative_count:
            return "disputed"
        if source_count >= 2 and (positive_count or negative_count or uncertain_count):
            return "corroborated"
        return "single_source"

    def _claim_support_strength(self, relationship_state: str, source_count: int) -> str:
        if relationship_state == "disputed":
            return "contested"
        if relationship_state == "corroborated" and source_count >= 3:
            return "high"
        if relationship_state == "corroborated":
            return "medium"
        return "low"

    def _claim_analysis_record(
        self,
        claim: EvidenceClaim,
        source_by_id: dict[str, EvidenceSource],
    ) -> dict[str, object]:
        source_ids = [
            source_id
            for source_id in claim.evidence_refs
            if self._is_external_source(source_by_id.get(source_id))
        ]
        source_scores = [
            source_by_id[source_id].confidence
            for source_id in source_ids
            if source_id in source_by_id
        ]
        return {
            "claim_id": claim.id,
            "text": claim.text,
            "claim_type": claim.claim_type,
            "confidence": claim.confidence,
            "source_ids": source_ids,
            "source_titles": [
                source_by_id[source_id].title
                for source_id in source_ids
                if source_id in source_by_id
            ],
            "terms": self._claim_terms(claim.text),
            "stance": self._claim_stance(claim.text, claim.claim_type),
            "facet": claim.extraction_metadata.get("claim_facet"),
            "source_confidence": max(source_scores) if source_scores else claim.confidence,
        }

    def _source_relationship_entry(
        self,
        relationship: str,
        left: dict[str, object],
        right: dict[str, object],
        shared_terms: list[str],
        basis: str,
        relationship_basis: str,
        facet_match: dict[str, object] | None = None,
    ) -> dict[str, object]:
        score = round(
            min(
                1.0,
                0.35
                + (0.07 * min(len(shared_terms), 5))
                + (float(left["confidence"]) * 0.14)
                + (float(right["confidence"]) * 0.14)
                + (float(left["source_confidence"]) * 0.15)
                + (float(right["source_confidence"]) * 0.15),
            ),
            3,
        )
        entry = {
            "relationship": relationship,
            "relationship_score": score,
            "shared_terms": shared_terms[:8],
            "stances": [left["stance"], right["stance"]],
            "claim_ids": [left["claim_id"], right["claim_id"]],
            "source_ids": sorted({*left["source_ids"], *right["source_ids"]}),
            "claim_texts": [left["text"], right["text"]],
            "source_titles": [*left["source_titles"], *right["source_titles"]],
            "basis": basis,
            "relationship_basis": relationship_basis,
        }
        if facet_match is not None:
            entry["facet_match"] = facet_match
        return entry

    def _facet_claim_relationship(
        self,
        left: dict[str, object],
        right: dict[str, object],
    ) -> dict[str, object] | None:
        left_facet = left.get("facet")
        right_facet = right.get("facet")
        if not isinstance(left_facet, dict) or not isinstance(right_facet, dict):
            return None
        if left_facet.get("relation") != right_facet.get("relation"):
            return None
        left_subject_terms = set(self._facet_topic_terms(str(left_facet.get("subject") or "")))
        right_subject_terms = set(self._facet_topic_terms(str(right_facet.get("subject") or "")))
        left_object_terms = set(self._facet_topic_terms(str(left_facet.get("object") or "")))
        right_object_terms = set(self._facet_topic_terms(str(right_facet.get("object") or "")))
        shared_subject_terms = sorted(left_subject_terms & right_subject_terms)
        shared_object_terms = sorted(left_object_terms & right_object_terms)
        if not shared_subject_terms or not shared_object_terms:
            return None
        relationship = self._claim_relationship(left["stance"], right["stance"])
        if relationship is None:
            return None
        return {
            "relationship": relationship,
            "shared_terms": sorted({*shared_subject_terms, *shared_object_terms}),
            "basis": (
                "Matched normalized source-claim facets across distinct external sources "
                "using subject, relation, object, and stance signals."
            ),
            "facet_match": {
                "relation": left_facet.get("relation"),
                "shared_subject_terms": shared_subject_terms,
                "shared_object_terms": shared_object_terms,
                "left_quantity": left_facet.get("quantity"),
                "right_quantity": right_facet.get("quantity"),
                "left_claim_id": left_facet.get("claim_id"),
                "right_claim_id": right_facet.get("claim_id"),
            },
        }

    def _different_sources(
        self,
        left: dict[str, object],
        right: dict[str, object],
    ) -> bool:
        return not set(left["source_ids"]).intersection(set(right["source_ids"]))

    def _claim_relationship(self, left_stance: object, right_stance: object) -> str | None:
        if left_stance == right_stance and left_stance in {"positive", "negative", "uncertain"}:
            return "agreement"
        if {left_stance, right_stance} == {"positive", "negative"}:
            return "conflict"
        return None

    def _claim_terms(self, text: str) -> list[str]:
        stop_words = {
            "about",
            "after",
            "again",
            "against",
            "assistant",
            "assistants",
            "because",
            "before",
            "being",
            "could",
            "from",
            "generated",
            "however",
            "into",
            "should",
            "software",
            "source",
            "teams",
            "their",
            "there",
            "these",
            "those",
            "through",
            "under",
            "when",
            "where",
            "which",
            "while",
            "with",
            "without",
        }
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        return sorted(
            {
                token
                for token in tokens
                if len(token) >= 4 and token not in stop_words and not token.isdigit()
            }
        )

    def _claim_stance(self, text: str, claim_type: str) -> str:
        lowered = text.lower()
        if claim_type == "uncertain" or self._looks_uncertain(text):
            return "uncertain"
        if claim_type == "disputed" or self._looks_disputed(text):
            return "contrast"
        positive_pattern = (
            r"\b(improv\w*|increase\w*|reduc\w*|benefit\w*|effective|faster|"
            r"higher|better|gain\w*|productiv\w*)\b"
        )
        negative_pattern = (
            r"\b(worsen\w*|declin\w*|harm\w*|fail\w*|risk\w*|lower|"
            r"overhead|error\w*|defect\w*)\b"
        )
        negated_positive_pattern = (
            r"\b(no|not|never|without|failed to|did not|does not)\s+"
            r"(improv\w*|increase\w*|reduc\w*|benefit\w*|help\w*)"
        )
        if re.search(negated_positive_pattern, lowered):
            return "negative"
        if re.search(positive_pattern, lowered):
            return "positive"
        if re.search(negative_pattern, lowered):
            return "negative"
        return "neutral"

    def _is_external_source(self, source: EvidenceSource | None) -> bool:
        return source is not None and not source.source_type.startswith("episode_")

    def _source_score(self, source: ResearchSourceInput) -> tuple[float, dict[str, object]]:
        confidence = source.confidence if source.confidence is not None else 0.55
        factors: dict[str, object] = {"base_confidence": confidence}
        source_type = source.source_type.lower()
        if source_type in {"government_report", "standards_body", "academic_paper"}:
            confidence += 0.18
            factors["authority_bonus"] = 0.18
        elif source_type in {"industry_report", "official_documentation"}:
            confidence += 0.12
            factors["authority_bonus"] = 0.12
        elif source_type in {"news_article", "manual_source"}:
            confidence += 0.04
            factors["authority_bonus"] = 0.04
        parsed = urlparse(source.uri or "")
        host = parsed.netloc.lower()
        if host.endswith(".gov") or host.endswith(".edu"):
            confidence += 0.08
            factors["domain_bonus"] = 0.08
        year = self._published_year(source.published_at)
        if year is not None:
            age = max(0, datetime.now(UTC).year - year)
            recency_bonus = max(0.0, 0.08 - (0.01 * age))
            confidence += recency_bonus
            factors["published_year"] = year
            factors["recency_bonus"] = round(recency_bonus, 3)
        final = max(0.0, min(1.0, round(confidence, 3)))
        factors["final_confidence"] = final
        return final, factors

    def _published_year(self, value: str | None) -> int | None:
        if not value:
            return None
        match = re.search(r"(19|20)\d{2}", value)
        return int(match.group(0)) if match else None

    def _source_summary(self, content: str) -> str:
        sentence = next(iter(self._sentences(content)), "")
        return sentence[:280]

    def _claims_from_source(
        self,
        source: ResearchSourceInput,
        source_id: str,
        topic: str,
        dimensions: list[str],
    ) -> dict[str, object]:
        supported: list[EvidenceClaim] = []
        statistics: list[EvidenceClaim] = []
        verified_facts: list[EvidenceClaim] = []
        uncertain: list[EvidenceClaim] = []
        disputed: list[EvidenceClaim] = []
        competing_interpretations: list[EvidenceClaim] = []
        advanced_counts: dict[str, int] = {
            "definition": 0,
            "mechanism": 0,
            "recommendation": 0,
            "tradeoff": 0,
            "relationship_facet": 0,
            "causal_context": 0,
            "scope_qualifier": 0,
        }
        relevant_terms = {self._normalize_token(item) for item in dimensions}
        relevant_terms.update(self._normalize_token(item) for item in topic.split())
        relevant_terms = {item for item in relevant_terms if len(item) > 3}
        for index, sentence in enumerate(self._sentences(source.content), start=1):
            normalized = self._normalize_token(sentence)
            if relevant_terms and not any(term in normalized for term in relevant_terms):
                continue
            claim = EvidenceClaim(
                id=f"{source_id}-claim-{index}",
                text=sentence,
                claim_type="source_supported",
                confidence=self._source_score(source)[0],
                evidence_refs=[source_id],
            )
            facet = self._claim_facet(sentence, source_id, claim.id)
            if facet is not None:
                claim.extraction_metadata["claim_facet"] = facet
                advanced_counts["relationship_facet"] += 1
            causal_context = self._causal_context(sentence, source_id, claim.id)
            if causal_context is not None:
                claim.extraction_metadata["causal_context"] = causal_context
                advanced_counts["causal_context"] += 1
            scope_qualifier = self._scope_qualifier(sentence, source_id, claim.id)
            if scope_qualifier is not None:
                claim.extraction_metadata["scope_qualifier"] = scope_qualifier
                advanced_counts["scope_qualifier"] += 1
            advanced_kind = self._advanced_fact_kind(sentence)
            if advanced_kind == "definition":
                claim.claim_type = "definition"
                claim.uncertainty = "Extracted by deterministic definition pattern."
                verified_facts.append(claim)
                advanced_counts["definition"] += 1
            elif advanced_kind in {"mechanism", "recommendation"}:
                claim.claim_type = advanced_kind
                claim.uncertainty = f"Extracted by deterministic {advanced_kind} fact pattern."
                verified_facts.append(claim)
                advanced_counts[advanced_kind] += 1
            elif advanced_kind == "tradeoff":
                claim.claim_type = "competing_interpretation"
                claim.uncertainty = (
                    "Source language presents a tradeoff or competing interpretation."
                )
                competing_interpretations.append(claim)
                advanced_counts["tradeoff"] += 1
            elif self._looks_statistical(sentence):
                claim.claim_type = "statistic"
                statistics.append(claim)
            elif self._looks_uncertain(sentence):
                claim.claim_type = "uncertain"
                claim.uncertainty = "Source language contains uncertainty or conditional wording."
                uncertain.append(claim)
            elif self._looks_disputed(sentence):
                claim.claim_type = "disputed"
                claim.uncertainty = "Source language indicates a contrast or disagreement."
                disputed.append(claim)
            else:
                supported.append(claim)
            extracted_count = (
                len(supported)
                + len(statistics)
                + len(verified_facts)
                + len(uncertain)
                + len(disputed)
                + len(competing_interpretations)
            )
            if extracted_count >= 12:
                break
        return {
            "supported": supported,
            "statistics": statistics,
            "verified_facts": verified_facts,
            "uncertain": uncertain,
            "disputed": disputed,
            "competing_interpretations": competing_interpretations,
            "advanced_counts": advanced_counts,
        }

    def _advanced_claims_from_source(
        self,
        source: ResearchSourceInput,
        source_id: str,
        topic: str,
        dimensions: list[str],
        extraction_index: int,
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        if not getattr(self.settings, "research_advanced_extraction_enabled", False):
            return self._empty_extracted_claims(), None
        extracted = self._empty_extracted_claims(
            advanced_counts={
                "external_model_verified_fact": 0,
                "external_model_supported_claim": 0,
                "external_model_statistic": 0,
                "external_model_uncertain": 0,
                "external_model_disputed": 0,
                "external_model_competing_interpretation": 0,
            }
        )
        max_sources = getattr(self.settings, "research_advanced_extraction_max_sources", 8)
        if extraction_index > max_sources:
            return extracted, {
                "tool": "advanced_claim_extractor",
                "status": "skipped",
                "error": "source_limit_reached",
                "source_id": source_id,
                "completed_at": datetime.now(UTC).isoformat(),
            }
        endpoint = getattr(self.settings, "research_advanced_extraction_url", None)
        started_at = datetime.now(UTC)
        start = time.monotonic()
        log_entry: dict[str, object] = {
            "tool": "advanced_claim_extractor",
            "policy": "source_bound_external_extractor_v1",
            "source_id": source_id,
            "source_type": source.source_type,
            "started_at": started_at.isoformat(),
        }
        if not endpoint:
            log_entry.update(
                {
                    "status": "skipped",
                    "error": "advanced_extraction_not_configured",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                }
            )
            return extracted, log_entry
        payload = {
            "schema_version": "research_advanced_extraction_request.v1",
            "topic": topic,
            "required_dimensions": dimensions,
            "allowed_evidence_refs": [source_id],
            "source": {
                "id": source_id,
                "title": source.title,
                "uri": source.uri,
                "source_type": source.source_type,
                "published_at": source.published_at,
                "summary": source.summary,
                "content": source.content[: min(len(source.content), 12_000)],
            },
            "output_contract": {
                "claims": [
                    {
                        "text": "string",
                        "claim_type": (
                            "verified_fact | supported | statistic | uncertain | disputed | "
                            "competing_interpretation"
                        ),
                        "confidence": "0..1",
                        "evidence_refs": [source_id],
                    }
                ]
            },
        }
        try:
            response = self._post_advanced_extraction_request(endpoint, payload)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            log_entry.update(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "message": str(exc)[:240],
                    "completed_at": datetime.now(UTC).isoformat(),
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                }
            )
            return extracted, log_entry
        accepted_claims, invalid_count = self._advanced_claims_from_payload(
            payload=response,
            source_id=source_id,
            source_confidence=self._source_score(source)[0],
        )
        for claim in accepted_claims:
            self._append_advanced_claim(extracted, claim)
        log_entry.update(
            {
                "status": "succeeded",
                "http_status": response.get("http_status"),
                "content_type": response.get("content_type"),
                "byte_count": response.get("byte_count"),
                "accepted_claim_count": len(accepted_claims),
                "invalid_claim_count": invalid_count,
                "completed_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": int((time.monotonic() - start) * 1000),
            }
        )
        return extracted, log_entry

    def _empty_extracted_claims(
        self,
        advanced_counts: dict[str, int] | None = None,
    ) -> dict[str, object]:
        return {
            "supported": [],
            "statistics": [],
            "verified_facts": [],
            "uncertain": [],
            "disputed": [],
            "competing_interpretations": [],
            "advanced_counts": advanced_counts or {},
        }

    def _advanced_claims_from_payload(
        self,
        payload: dict[str, object],
        source_id: str,
        source_confidence: float,
    ) -> tuple[list[EvidenceClaim], int]:
        raw_claims = payload.get("claims", [])
        if not isinstance(raw_claims, list):
            return [], 1
        accepted: list[EvidenceClaim] = []
        invalid_count = 0
        max_claims = getattr(self.settings, "research_advanced_extraction_max_claims_per_source", 6)
        for index, item in enumerate(raw_claims, start=1):
            if len(accepted) >= max_claims:
                break
            if not isinstance(item, dict):
                invalid_count += 1
                continue
            claim = self._advanced_claim_from_item(
                item=item,
                source_id=source_id,
                source_confidence=source_confidence,
                index=index,
            )
            if claim is None:
                invalid_count += 1
                continue
            accepted.append(claim)
        return accepted, invalid_count

    def _advanced_claim_from_item(
        self,
        item: dict[str, object],
        source_id: str,
        source_confidence: float,
        index: int,
    ) -> EvidenceClaim | None:
        text = str(item.get("text") or "").strip()
        if len(text.split()) < 4:
            return None
        raw_refs = item.get("evidence_refs")
        evidence_refs = (
            [ref for ref in raw_refs if isinstance(ref, str)] if isinstance(raw_refs, list) else []
        )
        if item.get("source_id") == source_id and not evidence_refs:
            evidence_refs = [source_id]
        if evidence_refs != [source_id]:
            return None
        claim_type = self._normalized_advanced_claim_type(str(item.get("claim_type") or ""))
        if claim_type is None:
            return None
        confidence = item.get("confidence")
        normalized_confidence = (
            max(0.0, min(1.0, float(confidence)))
            if isinstance(confidence, int | float)
            else source_confidence
        )
        claim_id = str(item.get("id") or f"{source_id}-advanced-claim-{index}")
        return EvidenceClaim(
            id=claim_id,
            text=text,
            claim_type=claim_type,
            confidence=round(min(normalized_confidence, source_confidence), 3),
            evidence_refs=[source_id],
            uncertainty=(
                "Extracted by source-bound external advanced extractor; retained for review."
            ),
            extraction_metadata={
                "advanced_extraction_policy": "source_bound_external_extractor_v1",
                "provider_claim_type": str(item.get("claim_type") or ""),
                "source_bound": True,
            },
        )

    def _normalized_advanced_claim_type(self, claim_type: str) -> str | None:
        normalized = claim_type.strip().lower()
        aliases = {
            "verified": "verified_fact",
            "verified_fact": "verified_fact",
            "fact": "verified_fact",
            "factual": "verified_fact",
            "supported": "source_supported",
            "source_supported": "source_supported",
            "statistic": "statistic",
            "statistics": "statistic",
            "uncertain": "uncertain",
            "disputed": "disputed",
            "conflict": "disputed",
            "competing": "competing_interpretation",
            "competing_interpretation": "competing_interpretation",
            "tradeoff": "competing_interpretation",
        }
        return aliases.get(normalized)

    def _append_advanced_claim(
        self,
        extracted: dict[str, object],
        claim: EvidenceClaim,
    ) -> None:
        if claim.claim_type == "statistic":
            extracted["statistics"].append(claim)
            extracted["advanced_counts"]["external_model_statistic"] += 1
        elif claim.claim_type == "uncertain":
            extracted["uncertain"].append(claim)
            extracted["advanced_counts"]["external_model_uncertain"] += 1
        elif claim.claim_type == "disputed":
            extracted["disputed"].append(claim)
            extracted["advanced_counts"]["external_model_disputed"] += 1
        elif claim.claim_type == "competing_interpretation":
            extracted["competing_interpretations"].append(claim)
            extracted["advanced_counts"]["external_model_competing_interpretation"] += 1
        elif claim.claim_type == "verified_fact":
            extracted["verified_facts"].append(claim)
            extracted["advanced_counts"]["external_model_verified_fact"] += 1
        else:
            extracted["supported"].append(claim)
            extracted["advanced_counts"]["external_model_supported_claim"] += 1

    def _claim_facets_from_claims(self, claims: list[EvidenceClaim]) -> list[dict[str, object]]:
        facets: list[dict[str, object]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for claim in claims:
            facet = claim.extraction_metadata.get("claim_facet")
            if not isinstance(facet, dict):
                continue
            key = (
                str(facet.get("subject") or ""),
                str(facet.get("relation") or ""),
                str(facet.get("object") or ""),
                str(facet.get("source_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            facets.append(facet)
        return facets

    def _causal_scope_contexts_from_claims(
        self,
        claims: list[EvidenceClaim],
    ) -> list[dict[str, object]]:
        contexts: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for claim in claims:
            for key, context_type in (
                ("causal_context", "causal"),
                ("scope_qualifier", "scope"),
            ):
                context = claim.extraction_metadata.get(key)
                if not isinstance(context, dict):
                    continue
                source_id = str(context.get("source_id") or "")
                normalized_key = (
                    context_type,
                    source_id,
                    self._normalize_token(
                        " ".join(
                            str(context.get(part) or "") for part in ("cause", "effect", "scope")
                        )
                    ),
                )
                if normalized_key in seen:
                    continue
                seen.add(normalized_key)
                contexts.append(context)
        return contexts[:24]

    def _sentences(self, content: str) -> list[str]:
        normalized = " ".join(content.split())
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", normalized)
            if sentence.strip()
        ]
        return [sentence for sentence in sentences if len(sentence.split()) >= 5]

    def _looks_statistical(self, sentence: str) -> bool:
        return bool(re.search(r"\b\d+([.,]\d+)?\s*(%|percent|million|billion|x)\b", sentence))

    def _claim_facet(
        self,
        sentence: str,
        source_id: str,
        claim_id: str,
    ) -> dict[str, object] | None:
        normalized_sentence = " ".join(sentence.strip().rstrip(".!?").split())
        if not normalized_sentence:
            return None
        relation_pattern = (
            r"\b(?P<relation>"
            r"improves?|improved|increases?|increased|raises?|raised|"
            r"reduces?|reduced|decreases?|decreased|lowers?|lowered|"
            r"causes?|caused|enables?|enabled|drives?|drove|"
            r"leads?\s+to|led\s+to|depends?\s+on|requires?|required|"
            r"supports?|supported|prevents?|prevented"
            r")\b"
        )
        match = re.search(relation_pattern, normalized_sentence, flags=re.IGNORECASE)
        if match is None:
            return None
        subject = self._facet_fragment(normalized_sentence[: match.start()])
        remainder = normalized_sentence[match.end() :]
        object_text = self._facet_object_fragment(remainder)
        if not subject or not object_text:
            return None
        quantity = self._quantity_expression(normalized_sentence)
        return {
            "claim_id": claim_id,
            "source_id": source_id,
            "subject": subject,
            "relation": self._normalized_relation(match.group("relation")),
            "object": object_text,
            "quantity": quantity,
            "topic_terms": self._facet_topic_terms(f"{subject} {object_text}"),
            "extraction_policy": "deterministic_relation_quantity_facets_v1",
        }

    def _causal_context(
        self,
        sentence: str,
        source_id: str,
        claim_id: str,
    ) -> dict[str, object] | None:
        normalized_sentence = " ".join(sentence.strip().rstrip(".!?").split())
        if not normalized_sentence:
            return None
        patterns = [
            (
                "because",
                r"^(?P<effect>.+?)\s+because\s+(?P<cause>.+)$",
                "cause_after_effect",
            ),
            (
                "due_to",
                r"^(?P<effect>.+?)\s+due\s+to\s+(?P<cause>.+)$",
                "cause_after_effect",
            ),
            (
                "as_a_result",
                r"^(?P<cause>.+?),?\s+as\s+a\s+result,?\s+(?P<effect>.+)$",
                "cause_before_effect",
            ),
            (
                "therefore",
                r"^(?P<cause>.+?),?\s+therefore,?\s+(?P<effect>.+)$",
                "cause_before_effect",
            ),
            (
                "leads_to",
                r"^(?P<cause>.+?)\s+(leads?\s+to|led\s+to)\s+(?P<effect>.+)$",
                "cause_before_effect",
            ),
        ]
        for connector, pattern, direction in patterns:
            match = re.search(pattern, normalized_sentence, flags=re.IGNORECASE)
            if match is None:
                continue
            cause = self._context_fragment(match.group("cause"))
            effect = self._context_fragment(match.group("effect"))
            if not cause or not effect:
                continue
            return {
                "context_type": "causal",
                "claim_id": claim_id,
                "source_id": source_id,
                "connector": connector,
                "direction": direction,
                "cause": cause,
                "effect": effect,
                "topic_terms": self._facet_topic_terms(f"{cause} {effect}"),
                "extraction_policy": "deterministic_causal_scope_context_v1",
            }
        return None

    def _scope_qualifier(
        self,
        sentence: str,
        source_id: str,
        claim_id: str,
    ) -> dict[str, object] | None:
        normalized_sentence = " ".join(sentence.strip().rstrip(".!?").split())
        patterns = [
            r"\b(?P<preposition>among|within|across|during)\s+(?P<scope>[^.;,]+)",
            (
                r"\b(?P<preposition>in|for)\s+(?P<scope>"
                r"((a|an|the)\s+)?"
                r"(regulated|controlled|field|high-risk|safety-critical|enterprise|"
                r"public-sector|healthcare|financial|software|engineering|maintenance)"
                r"[^.;,]*)"
            ),
            r"\b(?P<preposition>when)\s+(?P<scope>[^.;,]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized_sentence, flags=re.IGNORECASE)
            if match is None:
                continue
            scope = self._context_fragment(match.group("scope"))
            if not scope or len(scope.split()) < 2:
                continue
            return {
                "context_type": "scope",
                "claim_id": claim_id,
                "source_id": source_id,
                "preposition": match.group("preposition").lower(),
                "scope": scope,
                "topic_terms": self._facet_topic_terms(scope),
                "extraction_policy": "deterministic_causal_scope_context_v1",
            }
        return None

    def _context_fragment(self, value: str) -> str:
        fragment = re.split(
            r"\b(however|although|while|whereas|but)\b|[;:]",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        fragment = fragment.strip(" ,.-")
        words = fragment.split()
        if len(words) > 12:
            words = words[:12]
        return " ".join(words).strip(" ,.-")

    def _facet_fragment(self, value: str) -> str:
        fragment = value.strip(" ,;:-")
        fragment = re.sub(
            r"^(however|although|while|whereas|because|when|if|and|but)\b[\s,]*",
            "",
            fragment,
            flags=re.IGNORECASE,
        )
        fragment = re.sub(
            r"\b(did not|does not|do not|not|never|failed to)\s*$",
            "",
            fragment,
            flags=re.IGNORECASE,
        )
        words = fragment.split()
        if len(words) > 8:
            words = words[-8:]
        return " ".join(words).strip(" ,;:-")

    def _facet_object_fragment(self, value: str) -> str:
        fragment = re.split(
            r"\b(because|when|while|whereas|although|however|but|and teams must|;)\b"
            r"|,\s*(teams|organizations|leaders|developers)\s+(must|should|need)\b",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        fragment = re.sub(
            r"\bby\s+\d+([.,]\d+)?\s*(%|percent|million|billion|x)\b.*$",
            "",
            fragment,
            flags=re.IGNORECASE,
        )
        fragment = re.split(
            r"\s+\b(in|for|among)\s+"
            r"(controlled|field|regulated|software|engineering|maintenance)\b",
            fragment,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        words = fragment.strip(" ,;:-").split()
        if len(words) > 10:
            words = words[:10]
        return " ".join(words).strip(" ,;:-")

    def _quantity_expression(self, sentence: str) -> str | None:
        match = re.search(
            r"\b\d+([.,]\d+)?\s*(%|percent|million|billion|x)\b",
            sentence,
            flags=re.IGNORECASE,
        )
        return match.group(0) if match else None

    def _normalized_relation(self, relation: str) -> str:
        lowered = " ".join(relation.lower().split())
        if lowered.startswith("improv"):
            return "improves"
        if lowered.startswith(("increas", "rais")):
            return "increases"
        if lowered.startswith(("reduc", "decreas", "lower")):
            return "reduces"
        if lowered.startswith("caus"):
            return "causes"
        if lowered.startswith("enabl"):
            return "enables"
        if lowered.startswith(("driv", "drove")):
            return "drives"
        if "lead" in lowered or lowered == "led to":
            return "leads_to"
        if "depend" in lowered:
            return "depends_on"
        if lowered.startswith("requir"):
            return "requires"
        if lowered.startswith("support"):
            return "supports"
        if lowered.startswith("prevent"):
            return "prevents"
        return lowered.replace(" ", "_")

    def _facet_topic_terms(self, value: str) -> list[str]:
        stop_words = {
            "about",
            "after",
            "before",
            "between",
            "from",
            "into",
            "that",
            "their",
            "there",
            "these",
            "this",
            "through",
            "with",
        }
        tokens = re.findall(r"[a-z][a-z0-9-]{3,}", value.lower())
        return sorted({token for token in tokens if token not in stop_words})[:8]

    def _advanced_fact_kind(self, sentence: str) -> str | None:
        if self._looks_definition(sentence):
            return "definition"
        if self._looks_mechanism(sentence):
            return "mechanism"
        if self._looks_tradeoff(sentence):
            return "tradeoff"
        if self._looks_recommendation(sentence):
            return "recommendation"
        return None

    def _looks_definition(self, sentence: str) -> bool:
        return bool(
            re.search(
                r"\b(is defined as|refers to|means|is a type of|is an approach to)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )

    def _looks_mechanism(self, sentence: str) -> bool:
        return bool(
            re.search(
                r"\b(because|by using|through|via|enables|causes|drives|leads to|"
                r"depends on|requires)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )

    def _looks_recommendation(self, sentence: str) -> bool:
        return bool(
            re.search(
                r"\b(should|must|recommend\w*|require\w*|need to|needs to|"
                r"best practice|guideline)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )

    def _looks_tradeoff(self, sentence: str) -> bool:
        return bool(
            re.search(
                r"\b(trade[- ]?off|on the other hand|while|whereas|balance between|"
                r"tension between)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )

    def _looks_uncertain(self, sentence: str) -> bool:
        return bool(
            re.search(
                r"\b(may|might|could|uncertain|unknown|risk|estimate|estimated)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )

    def _looks_disputed(self, sentence: str) -> bool:
        return bool(
            re.search(
                r"\b(however|although|but|disagree|conflict|contradict|disputed)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )

    def _source_dedupe_key(self, title: str, uri: str | None) -> str:
        if uri:
            return f"uri:{self._normalized_uri(uri)}"
        return f"title:{self._slug(title)}"

    def _normalized_uri(self, uri: str) -> str:
        parsed = urlparse(uri)
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"

    def _unique_source_id(self, source_id: str, seen_source_ids: set[str]) -> str:
        if source_id not in seen_source_ids:
            return source_id
        index = 2
        while f"{source_id}-{index}" in seen_source_ids:
            index += 1
        return f"{source_id}-{index}"

    def _normalize_token(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    def _slug(self, value: str) -> str:
        normalized = "".join(
            character.lower() if character.isalnum() else "-" for character in value.strip()
        )
        parts = [part for part in normalized.split("-") if part]
        return "-".join(parts) or "item"
