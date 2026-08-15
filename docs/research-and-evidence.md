# Research and Evidence

Increment 5 now has the first evidence-pack path with explicit URL retrieval and
operator-configured live source discovery.
It creates a versioned, checksummed `evidence_pack` asset from the episode
topic, required dimensions, scope exclusions, research policy, supplied source
material, producer-provided retrieval targets, and discovered search results.
Configuration sources are labelled separately from manual/external sources so
QC can distinguish workflow grounding from retrieved research.

`POST /api/v1/episodes/{episode_id}/research/build` creates the evidence pack.
The request accepts `user_id`, `regenerate`, optional `require_approval`, an
optional `sources` list, an optional `retrieval_targets` list,
`discover_sources`, and optional `discovery_queries`. Each supplied source can
include title, URI, source type, author, published date, confidence hint,
summary, and source text. Retrieval targets contain explicit HTTP(S) URLs plus
source metadata; the service fetches those targets, extracts text from plain
text, JSON, or simple HTML payloads, applies the same source scoring path, and
records a structured `retrieval_tool_log` in evidence asset metadata.
Unsupported URI schemes, timeouts, and HTTP failures are logged without
silently creating sources.

When `discover_sources` is true and
`DIALECTICORE_RESEARCH_DISCOVERY_ENABLED` plus
`DIALECTICORE_RESEARCH_DISCOVERY_URL_TEMPLATE` are configured, the service
builds deduplicated search queries from `discovery_queries` or the episode
topic/dimensions. It calls the configured discovery endpoint with `{query}`
expanded, extracts HTTP(S) result URLs from JSON (`results`, `items`,
`organic_results`, or `webPages.value`) or simple HTML links, records a
structured `discovery_tool_log`, and then fetches selected discovered targets
through the same retrieval path. Discovery result provenance, including query
and rank, is retained in retrieval logs. Sources are de-duplicated by normalized
URI or title, scored from source type, domain, recency, and confidence hint, and
stored with content checksum evidence. When approval is required, the episode
enters `RESEARCH_REVIEW` and a normal `research_review` approval is created.
Approving research uses the existing approval decision endpoint and is blocked
if the latest evidence-pack QC fails. While that required approval is pending,
discussion generation does not start. If the review is rejected, discussion and
completion remain blocked until the evidence pack is rebuilt or revised and a
new required review is approved.

`POST /api/v1/episodes/{episode_id}/research/source-review` records a human
decision for one evidence source in the selected or latest evidence pack. The
request accepts `source_id`, `decision` (`approved`, `rejected`, or
`needs_revision`), optional `evidence_pack_asset_id`, `user_id`, and notes.
Review decisions are stored in `source_reviews`, summarized with
`human_source_review_v1`, surfaced in asset metadata, and audited with
`research.source_review.recorded`.

The evidence pack JSON uses `schema_version: evidence_pack.v1` and includes:

- topic definition
- generated research sub-questions
- definitions
- verified fact, supported, uncertain, disputed, competing-interpretation, and
  statistical claim buckets
- source index
- source rankings and source policy summary
- source reviews and source review summary
- cross-source agreement, conflict, and relationship-summary blocks
- suggested discussion dimensions
- fact-check rules

The current pack includes one source for the episode definition, one source per
required discussion dimension, any supplied external/manual sources, any
successfully retrieved URL sources, and any successfully fetched discovered
sources. Source text is scanned deterministically for relevant sentences,
statistics, uncertain language, disputed/contrast language, definitions,
mechanisms, recommendations, and tradeoff/comparison statements. Definitions,
mechanisms, and recommendations are recorded as `verified_facts`; tradeoff
statements are recorded as `competing_interpretations`. The pack also extracts
source-grounded relationship/quantity facets with the
`deterministic_relation_quantity_facets_v1` policy. Facets record the source
claim ID, source ID, normalized subject, relation, object, optional quantity,
and topical terms so downstream QC and reviewers can inspect claim structure
without relying only on a whole sentence. The `deterministic_fact_patterns_v1`
extraction policy, facet policy, and per-pattern counts are stored in the
evidence pack summary and QC details. Extracted claims are linked back to source
IDs. The service does not invent external facts when no source text is supplied
or when discovery/retrieval fails.

When `DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_ENABLED` and
`DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_URL` are configured, each accepted
source can also be sent to a trusted external advanced extractor. The extractor
must return source-bound claims; claims without the current source ID are
rejected and counted. Accepted external claims are inserted into the normal
verified, supported, statistic, uncertain, disputed, or competing-interpretation
buckets with `source_bound_external_extractor_v1` provenance. Tool logs record
attempts, successes, accepted claims, and rejected untrusted claims.
For source text, the service also extracts deterministic causal and scope
contexts with `deterministic_causal_scope_context_v1`. Causal contexts split
source-bound sentences into cause/effect records for connectors such as
`because`, `due to`, `therefore`, and `leads to`; scope contexts retain
qualifiers such as `in controlled teams`, `for software teams`, or `when code is
merged`. These records keep source ID and claim ID links and are counted in
evidence metadata and QC.
Sources are ranked with the deterministic
`confidence_authority_recency_checksum_v1` policy, which combines source
confidence, authority type/domain, publication recency, and content-checksum
presence. The pack records ranked source tiers plus a policy summary with
external-source count, strong-source count, highest-ranked source IDs, source
diversity, and stale-source evidence.

When two or more external sources produce extracted claims, the research service
also runs the deterministic `deterministic_shared_terms_stance_v1` cross-source
analysis. It compares extracted source claims from distinct sources using shared
topical terms and stance signals. It also applies
`deterministic_claim_facet_relationships_v1` to compare matching
subject/relation/object facets across distinct sources, separating facet-based
agreements from facet-based conflicts. It then builds
`deterministic_claim_support_groups_v1` support groups that consolidate related
claims across sources and label each group as corroborated, disputed, or
single-source. The pack records `source_agreements`, `source_conflicts`,
relationship basis, facet-match evidence, claim support groups, and
`cross_source_summary` counts. These summaries identify corroborated factual
ground and disputed claims without inventing facts outside the supplied or
retrieved source text.

`GET /api/v1/episodes/{episode_id}/research` returns the latest active
evidence-pack asset and JSON payload. Each saved evidence pack is also projected
into durable `ResearchSource` and `EvidenceClaim` rows. `GET
/api/v1/episodes/{episode_id}/research/sources` returns source records with URL,
publisher, published/retrieved timestamps, content hash, source type,
credibility score, and evidence-pack lineage metadata. `GET
/api/v1/episodes/{episode_id}/research/claims` returns claim records with
statement, type, confidence, status, supporting/contradicting source IDs, notes,
and extraction metadata. The discussion engine includes a concise public
evidence summary and available source IDs in each participant turn context. The
deterministic mock model cites the first available evidence source, which lets
the rest of the pipeline exercise source attribution without relying on network
retrieval.

`POST /api/v1/episodes/{episode_id}/research/claim-qc` checks the canonical
transcript, or a supplied transcript version, against the selected evidence
pack. Automated `qc-worker` passes only select an approved canonical/broadcast
transcript; manual calls with an explicit transcript version remain available
for operator review. It records `claim_citation_integrity` QC with claim counts,
cited claim counts, unsupported claim counts, invalid evidence reference counts,
and whether source links are required by the episode research policy.

The dashboard research panel shows source-review counts and can approve the
next unreviewed external source. Source-review QC records reviewed, approved,
rejected, needs-revision, and unreviewed external-source counts.

Remaining Increment 5 work:

- No known Increment 5 implementation gaps remain beyond stronger future
  semantic/embedding-assisted analysis and live source/tool ecosystem
  hardening.
