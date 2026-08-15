import pytest
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.enums import AssetType, EpisodeStatus
from app.domain.schemas import (
    EpisodeCreateRequest,
    EpisodeDefinition,
    ResearchBuildRequest,
    ResearchClaimQcRequest,
    ResearchSourceReviewRequest,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.discussion_engine import DiscussionEngine
from app.services.model_gateway import ModelGateway
from app.services.research_service import ResearchService


def research_definition() -> EpisodeDefinition:
    return EpisodeDefinition.model_validate(
        {
            "title": "Evidence grounded AI panel",
            "topic": {
                "central_question": "How should AI assistants be governed in software teams?",
                "required_dimensions": ["productivity", "risk", "quality"],
                "exclusions": ["sentience debates"],
            },
            "format": {"target_duration_minutes": 4, "participant_count": 4},
            "participants": [
                {"participant_profile_id": "host", "role": "moderator"},
                {"participant_profile_id": "optimist", "role": "panelist"},
                {"participant_profile_id": "skeptic", "role": "panelist"},
                {"participant_profile_id": "practitioner", "role": "panelist"},
            ],
            "research": {
                "enabled": True,
                "depth": "standard",
                "require_source_links": True,
                "approval_required": True,
            },
        }
    )


@pytest.mark.asyncio
async def test_research_pack_grounds_discussion_and_claim_qc(tmp_path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repository = EpisodeRepository()
    episode = repository.create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(user_id="tester"),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    evidence_qc = [
        result
        for result in researched.quality_results
        if result.check_type == "evidence_pack_integrity"
    ][-1]
    assert researched.status == EpisodeStatus.research_review
    assert researched.approvals[-1].stage == "research_review"
    assert evidence_asset.status == "completed"
    assert evidence_asset.checksum and evidence_asset.checksum.startswith("sha256:")
    assert evidence_asset.generation_metadata["source_count"] == 4
    assert evidence_qc.status == "warning"
    assert evidence_qc.details["retrieved_source_count"] == 0

    researched.approvals[-1].decision = "approved"
    researched.status = EpisodeStatus.draft
    produced = await DiscussionEngine(ModelGateway(), settings).run(researched)
    assert produced.discussion_session is not None
    assert all(
        turn.structured_output.claims[0].evidence_refs == ["episode-definition"]
        for turn in produced.discussion_session.turns
    )

    checked = service.run_claim_qc(
        produced,
        ResearchClaimQcRequest(user_id="tester"),
    )
    claim_qc = [
        result
        for result in checked.quality_results
        if result.check_type == "claim_citation_integrity"
    ][-1]
    assert claim_qc.status == "pass"
    assert claim_qc.details["claim_count"] == len(produced.discussion_session.turns)
    assert claim_qc.details["cited_claim_count"] == claim_qc.details["claim_count"]
    assert checked.audit_events[-1].event_type == "research.claim_qc.completed"


def test_research_pack_ingests_scores_dedupes_and_extracts_sources(tmp_path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            sources=[
                {
                    "title": "AI Engineering Governance Report",
                    "uri": "https://example.edu/reports/ai-governance",
                    "source_type": "academic_paper",
                    "published_at": "2026-02-01",
                    "content": (
                        "AI assistants improved productivity by 27 percent in a controlled "
                        "software maintenance study. However, quality outcomes depended on "
                        "review discipline and test coverage. Teams may face risk when generated "
                        "code is merged without ownership."
                    ),
                },
                {
                    "title": "Duplicate AI Engineering Governance Report",
                    "uri": "https://example.edu/reports/ai-governance/",
                    "source_type": "academic_paper",
                    "content": "This duplicate URI should not create a second source.",
                },
            ],
        ),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    pack = evidence_asset.generation_metadata["evidence_pack"]
    evidence_qc = [
        result
        for result in researched.quality_results
        if result.check_type == "evidence_pack_integrity"
    ][-1]
    external_sources = [
        source for source in pack["source_index"] if source["source_type"] == "academic_paper"
    ]
    rankings = pack["source_rankings"]
    assert len(external_sources) == 1
    assert external_sources[0]["confidence"] > 0.75
    assert external_sources[0]["content_checksum"].startswith("sha256:")
    assert external_sources[0]["score_factors"]["domain_bonus"] == 0.08
    assert rankings[0]["source_id"] == external_sources[0]["id"]
    assert rankings[0]["tier"] == "primary"
    assert rankings[0]["external"] is True
    assert pack["source_policy"]["strong_source_count"] == 1
    assert (
        evidence_asset.generation_metadata["highest_ranked_source_id"]
        == (external_sources[0]["id"])
    )
    assert evidence_qc.details["source_ranking_count"] == len(rankings)
    assert evidence_qc.details["strong_source_count"] == 1
    assert evidence_qc.status == "pass"
    assert evidence_qc.details["retrieved_source_count"] == 1
    assert any("27 percent" in claim["text"] for claim in pack["important_statistics"])
    assert evidence_asset.generation_metadata["scope_qualifier_count"] == 2
    assert pack["cross_source_summary"]["scope_qualifier_count"] == 2
    assert pack["cross_source_summary"]["causal_scope_contexts"] == [
        {
            "context_type": "scope",
            "claim_id": "https-example-edu-reports-ai-governance-claim-1",
            "source_id": "https-example-edu-reports-ai-governance",
            "preposition": "in",
            "scope": "a controlled software maintenance study",
            "topic_terms": ["controlled", "maintenance", "software", "study"],
            "extraction_policy": "deterministic_causal_scope_context_v1",
        },
        {
            "context_type": "scope",
            "claim_id": "https-example-edu-reports-ai-governance-claim-3",
            "source_id": "https-example-edu-reports-ai-governance",
            "preposition": "when",
            "scope": "generated code is merged without ownership",
            "topic_terms": ["code", "generated", "merged", "ownership", "without"],
            "extraction_policy": "deterministic_causal_scope_context_v1",
        },
    ]
    assert evidence_qc.details["scope_qualifier_count"] == 2
    assert any(
        facet["relation"] == "improves" and facet["quantity"] == "27 percent"
        for facet in pack["cross_source_summary"]["claim_facets"]
    )
    assert any("may face risk" in claim["text"] for claim in pack["uncertain_claims"])
    assert any("However" in claim["text"] for claim in pack["disputed_claims"])


def test_research_pack_summarizes_cross_source_agreement_and_conflict(tmp_path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            sources=[
                {
                    "title": "Controlled Productivity Study",
                    "uri": "https://example.edu/studies/productivity",
                    "source_type": "academic_paper",
                    "published_at": "2026",
                    "content": (
                        "AI assistants improve productivity by 27 percent in controlled "
                        "software maintenance work."
                    ),
                },
                {
                    "title": "Government Productivity Survey",
                    "uri": "https://example.gov/reports/productivity",
                    "source_type": "government_report",
                    "published_at": "2026",
                    "content": (
                        "AI assistants improve productivity by 24 percent in field "
                        "software maintenance teams."
                    ),
                },
                {
                    "title": "Regulated Team Quality Report",
                    "uri": "https://example.org/reports/regulated-teams",
                    "source_type": "industry_report",
                    "published_at": "2026",
                    "content": (
                        "AI assistants did not improve productivity in regulated "
                        "software maintenance teams."
                    ),
                },
            ],
        ),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    pack = evidence_asset.generation_metadata["evidence_pack"]
    evidence_qc = [
        result
        for result in researched.quality_results
        if result.check_type == "evidence_pack_integrity"
    ][-1]

    assert pack["source_agreements"]
    assert pack["source_conflicts"]
    assert pack["cross_source_summary"]["analysis_policy"] == (
        "deterministic_shared_terms_stance_v1"
    )
    assert pack["cross_source_summary"]["agreement_count"] >= 1
    assert pack["cross_source_summary"]["conflict_count"] >= 1
    assert pack["cross_source_summary"]["facet_analysis_policy"] == (
        "deterministic_claim_facet_relationships_v1"
    )
    assert pack["cross_source_summary"]["facet_agreement_count"] >= 1
    assert pack["cross_source_summary"]["facet_conflict_count"] >= 1
    assert pack["cross_source_summary"]["claim_support_policy"] == (
        "deterministic_claim_support_groups_v1"
    )
    assert pack["cross_source_summary"]["claim_support_group_count"] >= 1
    assert pack["cross_source_summary"]["disputed_claim_group_count"] >= 1
    assert "productivity" in pack["cross_source_summary"]["relationship_terms"]
    productivity_group = next(
        group
        for group in pack["cross_source_summary"]["claim_support_groups"]
        if group["basis"] == "facet"
        and group["relation"] == "improves"
        and "productivity" in group["group_key"]
    )
    assert productivity_group["relationship_state"] == "disputed"
    assert productivity_group["source_count"] == 3
    assert productivity_group["stance_counts"]["positive"] == 2
    assert productivity_group["stance_counts"]["negative"] == 1
    assert any(
        agreement["relationship_basis"] == "claim_facet"
        and agreement["facet_match"]["relation"] == "improves"
        and agreement["facet_match"]["shared_object_terms"] == ["productivity"]
        for agreement in pack["source_agreements"]
    )
    assert any(
        conflict["relationship_basis"] == "claim_facet"
        and conflict["facet_match"]["relation"] == "improves"
        and conflict["facet_match"]["shared_object_terms"] == ["productivity"]
        for conflict in pack["source_conflicts"]
    )
    assert evidence_asset.generation_metadata["source_agreement_count"] >= 1
    assert evidence_asset.generation_metadata["source_conflict_count"] >= 1
    assert evidence_asset.generation_metadata["facet_agreement_count"] >= 1
    assert evidence_asset.generation_metadata["facet_conflict_count"] >= 1
    assert evidence_asset.generation_metadata["claim_support_group_count"] >= 1
    assert evidence_asset.generation_metadata["disputed_claim_group_count"] >= 1
    assert evidence_qc.details["source_agreement_count"] >= 1
    assert evidence_qc.details["source_conflict_count"] >= 1
    assert evidence_qc.details["facet_agreement_count"] >= 1
    assert evidence_qc.details["facet_conflict_count"] >= 1
    assert evidence_qc.details["claim_support_group_count"] >= 1
    assert evidence_qc.details["disputed_claim_group_count"] >= 1
    assert evidence_qc.details["cross_source_cluster_count"] >= 1
    assert evidence_qc.status == "pass"


def test_research_pack_extracts_structured_facts_and_interpretations(tmp_path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            sources=[
                {
                    "title": "AI Governance Practice Guide",
                    "uri": "https://example.edu/guides/ai-governance",
                    "source_type": "academic_paper",
                    "published_at": "2026",
                    "content": (
                        "AI governance is defined as a set of policies for software teams. "
                        "Independent review reduces quality failures because reviewers catch "
                        "unverified AI assistant output. Software teams should document ownership "
                        "before generated code is merged. While AI assistants improve throughput, "
                        "teams must balance speed with risk controls."
                    ),
                },
            ],
        ),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    evidence_qc = [
        result
        for result in researched.quality_results
        if result.check_type == "evidence_pack_integrity"
    ][-1]
    metadata = evidence_asset.generation_metadata
    pack = metadata["evidence_pack"]
    summary = pack["cross_source_summary"]

    assert metadata["verified_fact_count"] == 3
    assert metadata["competing_interpretation_count"] == 1
    assert metadata["advanced_extraction_policy"] == "deterministic_fact_patterns_v1"
    assert metadata["causal_scope_policy"] == "deterministic_causal_scope_context_v1"
    assert metadata["causal_context_count"] == 1
    assert metadata["scope_qualifier_count"] == 1
    assert metadata["claim_facet_policy"] == "deterministic_relation_quantity_facets_v1"
    assert metadata["claim_facet_count"] == 2
    assert summary["advanced_extraction_counts"] == {
        "definition": 1,
        "mechanism": 1,
        "recommendation": 1,
        "tradeoff": 1,
        "relationship_facet": 2,
        "causal_context": 1,
        "scope_qualifier": 1,
    }
    assert summary["causal_scope_policy"] == "deterministic_causal_scope_context_v1"
    assert summary["causal_context_count"] == 1
    assert summary["scope_qualifier_count"] == 1
    assert summary["causal_scope_contexts"] == [
        {
            "context_type": "scope",
            "claim_id": "https-example-edu-guides-ai-governance-claim-1",
            "source_id": "https-example-edu-guides-ai-governance",
            "preposition": "for",
            "scope": "software teams",
            "topic_terms": ["software", "teams"],
            "extraction_policy": "deterministic_causal_scope_context_v1",
        },
        {
            "context_type": "causal",
            "claim_id": "https-example-edu-guides-ai-governance-claim-2",
            "source_id": "https-example-edu-guides-ai-governance",
            "connector": "because",
            "direction": "cause_after_effect",
            "cause": "reviewers catch unverified AI assistant output",
            "effect": "Independent review reduces quality failures",
            "topic_terms": [
                "assistant",
                "catch",
                "failures",
                "independent",
                "output",
                "quality",
                "reduces",
                "review",
            ],
            "extraction_policy": "deterministic_causal_scope_context_v1",
        },
    ]
    assert summary["claim_facet_count"] == 2
    assert {
        (facet["subject"], facet["relation"], facet["object"]) for facet in summary["claim_facets"]
    } == {
        ("Independent review", "reduces", "quality failures"),
        ("AI assistants", "improves", "throughput"),
    }
    assert {claim["claim_type"] for claim in pack["verified_facts"]} == {
        "definition",
        "mechanism",
        "recommendation",
    }
    mechanism_claim = next(
        claim for claim in pack["verified_facts"] if claim["claim_type"] == "mechanism"
    )
    assert mechanism_claim["extraction_metadata"]["claim_facet"]["relation"] == "reduces"
    assert pack["competing_interpretations"][0]["claim_type"] == ("competing_interpretation")
    assert evidence_qc.status == "pass"
    assert evidence_qc.details["verified_fact_count"] == 3
    assert evidence_qc.details["competing_interpretation_count"] == 1
    assert evidence_qc.details["advanced_extraction_policy"] == ("deterministic_fact_patterns_v1")
    assert evidence_qc.details["causal_scope_policy"] == ("deterministic_causal_scope_context_v1")
    assert evidence_qc.details["causal_context_count"] == 1
    assert evidence_qc.details["scope_qualifier_count"] == 1
    assert evidence_qc.details["claim_facet_count"] == 2


def test_research_pack_accepts_source_bound_external_advanced_extraction(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        research_advanced_extraction_enabled=True,
        research_advanced_extraction_url="https://extractor.example.local/research",
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)
    captured_payloads: list[dict[str, object]] = []

    def fake_extract(uri: str, payload: dict[str, object]) -> dict[str, object]:
        assert uri == "https://extractor.example.local/research"
        captured_payloads.append(payload)
        source_id = payload["allowed_evidence_refs"][0]
        return {
            "claims": [
                {
                    "text": "Independent source review improves AI governance reliability.",
                    "claim_type": "verified_fact",
                    "confidence": 0.91,
                    "evidence_refs": [source_id],
                },
                {
                    "text": "Governance teams reduced incident review time by 18 percent.",
                    "claim_type": "statistic",
                    "confidence": 0.88,
                    "source_id": source_id,
                },
                {
                    "text": "This claim cites no source and must be rejected.",
                    "claim_type": "verified_fact",
                    "confidence": 0.7,
                    "evidence_refs": [],
                },
                {
                    "text": "This claim cites an unknown source and must be rejected.",
                    "claim_type": "verified_fact",
                    "confidence": 0.7,
                    "evidence_refs": ["unknown-source"],
                },
            ],
        }

    monkeypatch.setattr(service, "_post_advanced_extraction_request", fake_extract)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            sources=[
                {
                    "title": "AI Governance Practice Guide",
                    "uri": "https://example.edu/guides/ai-governance",
                    "source_type": "academic_paper",
                    "published_at": "2026",
                    "content": (
                        "AI governance guidance helps software teams manage assistant risk."
                    ),
                },
            ],
        ),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    evidence_qc = [
        result
        for result in researched.quality_results
        if result.check_type == "evidence_pack_integrity"
    ][-1]
    metadata = evidence_asset.generation_metadata
    pack = metadata["evidence_pack"]
    summary = pack["cross_source_summary"]

    assert captured_payloads[0]["schema_version"] == ("research_advanced_extraction_request.v1")
    assert metadata["advanced_extraction_attempt_count"] == 1
    assert metadata["advanced_extraction_success_count"] == 1
    assert metadata["advanced_extraction_accepted_claim_count"] == 2
    assert metadata["advanced_extraction_invalid_claim_count"] == 2
    assert metadata["advanced_extraction_tool_log"][0]["status"] == "succeeded"
    assert metadata["advanced_extraction_tool_log"][0]["accepted_claim_count"] == 2
    assert summary["external_advanced_extraction_policy"] == ("source_bound_external_extractor_v1")
    assert summary["advanced_extraction_counts"]["external_model_verified_fact"] == 1
    assert summary["advanced_extraction_counts"]["external_model_statistic"] == 1
    assert any(
        claim["extraction_metadata"]["advanced_extraction_policy"]
        == "source_bound_external_extractor_v1"
        for claim in pack["verified_facts"]
    )
    assert any("18 percent" in claim["text"] for claim in pack["important_statistics"])
    assert evidence_qc.status == "warning"
    assert evidence_qc.details["advanced_extraction_accepted_claim_count"] == 2
    assert evidence_qc.details["advanced_extraction_invalid_claim_count"] == 2
    assert any(
        issue["issue"] == "advanced_extraction_rejected_untrusted_claims"
        for issue in evidence_qc.details["issues"]
    )


def test_research_source_review_records_human_decision_and_qc(tmp_path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            sources=[
                {
                    "title": "AI Governance Practice Guide",
                    "uri": "https://example.edu/guides/ai-governance",
                    "source_type": "academic_paper",
                    "published_at": "2026",
                    "content": (
                        "AI assistants improve productivity by 27 percent in software teams."
                    ),
                },
                {
                    "title": "AI Risk Operations Note",
                    "uri": "https://example.org/notes/ai-risk",
                    "source_type": "industry_report",
                    "published_at": "2026",
                    "content": (
                        "Teams may face risk when generated code is merged without ownership."
                    ),
                },
            ],
        ),
    )
    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    pack = evidence_asset.generation_metadata["evidence_pack"]
    reviewed_source_id = next(
        source["id"] for source in pack["source_index"] if source["source_type"] == "academic_paper"
    )

    reviewed = service.review_evidence_source(
        researched,
        ResearchSourceReviewRequest(
            user_id="reviewer",
            source_id=reviewed_source_id,
            decision="approved",
            notes="Matches accepted source policy.",
        ),
    )

    updated_pack = evidence_asset.generation_metadata["evidence_pack"]
    review_qc = [
        result
        for result in reviewed.quality_results
        if result.check_type == "research_source_review_integrity"
    ][-1]
    assert evidence_asset.generation_metadata["source_review_policy"] == ("human_source_review_v1")
    assert evidence_asset.generation_metadata["source_review_count"] == 1
    assert evidence_asset.generation_metadata["approved_source_count"] == 1
    assert evidence_asset.generation_metadata["unreviewed_external_source_count"] == 1
    assert updated_pack["source_reviews"][0]["source_id"] == reviewed_source_id
    assert updated_pack["source_reviews"][0]["decision"] == "approved"
    assert updated_pack["source_review_summary"]["approved_source_ids"] == [reviewed_source_id]
    assert review_qc.status == "warning"
    assert review_qc.details["review_policy"] == "human_source_review_v1"
    assert review_qc.details["approved_source_count"] == 1
    assert review_qc.details["unreviewed_external_source_count"] == 1
    assert reviewed.audit_events[-1].event_type == "research.source_review.recorded"
    assert reviewed.audit_events[-1].details["source_id"] == reviewed_source_id


def test_research_pack_retrieves_url_sources_and_records_tool_logs(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        research_retrieval_timeout_seconds=1,
        research_retrieval_max_bytes=4096,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)

    def fake_fetch(uri: str) -> dict[str, object]:
        assert uri == "https://research.example.org/ai-governance"
        return {
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "byte_count": 198,
            "payload": (
                b"<html><body><article><p>AI assistants reduced review wait time "
                b"by 31 percent for software teams.</p><p>However, quality teams "
                b"disagreed about governance overhead.</p></article></body></html>"
            ),
        }

    monkeypatch.setattr(service, "_fetch_retrieval_target", fake_fetch)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            retrieval_targets=[
                {
                    "title": "Retrieved AI Governance Page",
                    "uri": "https://research.example.org/ai-governance",
                    "source_type": "government_report",
                    "published_at": "2026",
                },
                {
                    "uri": "file:///etc/passwd",
                    "source_type": "manual_source",
                },
            ],
        ),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    metadata = evidence_asset.generation_metadata
    pack = metadata["evidence_pack"]
    retrieval_log = metadata["retrieval_tool_log"]
    external_sources = [
        source for source in pack["source_index"] if source["source_type"] == "government_report"
    ]
    assert metadata["retrieval_attempt_count"] == 2
    assert metadata["retrieval_success_count"] == 1
    assert metadata["retrieval_failure_count"] == 1
    assert retrieval_log[0]["status"] == "succeeded"
    assert retrieval_log[0]["tool"] == "http_get"
    assert retrieval_log[0]["content_checksum"].startswith("sha256:")
    assert retrieval_log[1]["status"] == "skipped"
    assert retrieval_log[1]["error"] == "unsupported_uri_scheme"
    assert len(external_sources) == 1
    assert external_sources[0]["content_checksum"].startswith("sha256:")
    assert external_sources[0]["score_factors"]["authority_bonus"] == 0.18
    assert any("31 percent" in claim["text"] for claim in pack["important_statistics"])
    assert any("However" in claim["text"] for claim in pack["disputed_claims"])


def test_research_pack_discovers_sources_from_configured_search_endpoint(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        research_retrieval_timeout_seconds=1,
        research_retrieval_max_bytes=4096,
        research_discovery_enabled=True,
        research_discovery_url_template="https://search.example.local/api?q={query}",
        research_discovery_max_queries=1,
        research_discovery_max_results_per_query=2,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ResearchService(settings)
    fetched_uris: list[str] = []

    def fake_fetch(uri: str) -> dict[str, object]:
        fetched_uris.append(uri)
        if uri.startswith("https://search.example.local/api"):
            assert "AI+governance+software+teams" in uri
            return {
                "http_status": 200,
                "content_type": "application/json",
                "byte_count": 420,
                "payload": (
                    b'{"results": ['
                    b'{"title": "Discovered Governance Study", '
                    b'"url": "https://example.edu/discovered-governance", '
                    b'"source_type": "academic_paper", "published_at": "2026", '
                    b'"snippet": "Empirical governance result."},'
                    b'{"name": "Discovered Risk Survey", '
                    b'"link": "https://example.gov/discovered-risk", '
                    b'"sourceType": "government_report", "datePublished": "2026", '
                    b'"description": "Risk evidence."}'
                    b"]}"
                ),
            }
        if uri == "https://example.edu/discovered-governance":
            return {
                "http_status": 200,
                "content_type": "text/html; charset=utf-8",
                "byte_count": 160,
                "payload": (
                    b"<html><body><p>AI governance reduced review failures by "
                    b"22 percent in software teams.</p></body></html>"
                ),
            }
        if uri == "https://example.gov/discovered-risk":
            return {
                "http_status": 200,
                "content_type": "text/plain",
                "byte_count": 190,
                "payload": (
                    b"AI assistants may create governance risk when software "
                    b"teams skip independent review."
                ),
            }
        raise AssertionError(f"unexpected uri {uri}")

    monkeypatch.setattr(service, "_fetch_retrieval_target", fake_fetch)

    researched = service.build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="tester",
            discover_sources=True,
            discovery_queries=["AI governance software teams"],
        ),
    )

    evidence_asset = next(
        asset for asset in researched.assets if asset.asset_type == AssetType.evidence_pack
    )
    evidence_qc = [
        result
        for result in researched.quality_results
        if result.check_type == "evidence_pack_integrity"
    ][-1]
    metadata = evidence_asset.generation_metadata
    pack = metadata["evidence_pack"]
    discovery_log = metadata["discovery_tool_log"]
    retrieval_log = metadata["retrieval_tool_log"]
    discovered_sources = [
        source
        for source in pack["source_index"]
        if source["uri"]
        in {"https://example.edu/discovered-governance", "https://example.gov/discovered-risk"}
    ]

    assert len(fetched_uris) == 3
    assert metadata["discovery_attempt_count"] == 1
    assert metadata["discovery_success_count"] == 1
    assert metadata["discovered_retrieval_target_count"] == 2
    assert discovery_log[0]["tool"] == "search_discovery"
    assert discovery_log[0]["status"] == "succeeded"
    assert discovery_log[0]["discovered_count"] == 2
    assert discovery_log[0]["selected_count"] == 2
    assert metadata["retrieval_success_count"] == 2
    assert all(entry["discovered_by"] == "search_discovery" for entry in retrieval_log)
    assert {entry["discovery_rank"] for entry in retrieval_log} == {1, 2}
    assert len(discovered_sources) == 2
    assert any("22 percent" in claim["text"] for claim in pack["important_statistics"])
    assert any("may create governance risk" in claim["text"] for claim in pack["uncertain_claims"])
    assert evidence_qc.status == "pass"
    assert evidence_qc.details["discovery_attempt_count"] == 1
    assert evidence_qc.details["discovery_success_count"] == 1
    assert evidence_qc.details["discovered_retrieval_target_count"] == 2


def test_discovery_classifies_known_public_primary_hosts_without_trusting_arbitrary_sites(
    tmp_path,
) -> None:
    service = ResearchService(Settings(object_storage_local_path=str(tmp_path / "object-store")))

    assert (
        service._source_type_for_uri("https://www.energy.gov/articles/data-centers")
        == "government_report"
    )
    assert (
        service._source_type_for_uri("https://www.iea.org/reports/energy-and-ai")
        == "industry_report"
    )
    assert service._source_type_for_uri("https://unverified-example.com/report") == "web_page"
