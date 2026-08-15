import json

import pytest
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.enums import AssetType, EpisodeStatus, TranscriptType, TurnType
from app.domain.schemas import (
    Approval,
    Asset,
    DiscussionSession,
    DiscussionTurn,
    EpisodeCreateRequest,
    EpisodeDefinition,
    StructuredTurnOutput,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.discussion_engine import DiscussionEngine
from app.services.model_gateway import ModelGateway, ModelResponse


def definition() -> EpisodeDefinition:
    return EpisodeDefinition.model_validate(
        {
            "title": "Will AI replace software developers?",
            "topic": {
                "central_question": "How will AI change professional software development?",
                "required_dimensions": ["productivity", "employment", "quality"],
            },
            "format": {"target_duration_minutes": 4, "participant_count": 4},
            "participants": [
                {"participant_profile_id": "host", "role": "moderator"},
                {"participant_profile_id": "optimist", "role": "panelist"},
                {"participant_profile_id": "skeptic", "role": "panelist"},
                {"participant_profile_id": "practitioner", "role": "panelist"},
            ],
        }
    )


def short_definition() -> EpisodeDefinition:
    return EpisodeDefinition.model_validate(
        {
            "title": "Fast AI panel",
            "topic": {
                "central_question": "How should teams use AI coding tools responsibly?",
                "required_dimensions": ["speed", "risk", "quality"],
            },
            "format": {
                    "target_duration_minutes": 1,
                    "permitted_deviation_percent": 0,
                    "participant_count": 4,
                    "maximum_monologue_seconds": 10,
            },
            "participants": [
                {"participant_profile_id": "host", "role": "moderator"},
                {"participant_profile_id": "optimist", "role": "panelist"},
                {"participant_profile_id": "skeptic", "role": "panelist"},
                {"participant_profile_id": "practitioner", "role": "panelist"},
            ],
        }
    )


def german_dimension_definition() -> EpisodeDefinition:
    return EpisodeDefinition.model_validate(
        {
            "title": "Frontier KI panel",
            "topic": {
                "central_question": "Welches Frontier-KI-Modell passt fuer Wissensarbeit?",
                "required_dimensions": [
                    "staerken",
                    "schwaechen",
                    "kosten_nutzen",
                    "einsatzempfehlung",
                ],
            },
            "format": {"target_duration_minutes": 4, "participant_count": 4},
            "participants": [
                {"participant_profile_id": "host", "role": "moderator"},
                {"participant_profile_id": "optimist", "role": "panelist"},
                {"participant_profile_id": "skeptic", "role": "panelist"},
                {"participant_profile_id": "practitioner", "role": "panelist"},
            ],
        }
    )


def frontier_cast_definition() -> EpisodeDefinition:
    return EpisodeDefinition.model_validate(
        {
            "title": "Frontier model roundtable",
            "topic": {
                "central_question": "Which frontier AI model is best suited for a talk show?",
                "required_dimensions": ["reasoning", "cost", "style"],
            },
            "format": {
                "target_duration_minutes": 1,
                "permitted_deviation_percent": 50,
                "participant_count": 6,
                "maximum_monologue_seconds": 10,
            },
            "participants": [
                {"participant_profile_id": "claude", "role": "moderator"},
                {"participant_profile_id": "chatgpt", "role": "panelist"},
                {"participant_profile_id": "deepseek", "role": "panelist"},
                {"participant_profile_id": "grok", "role": "panelist"},
                {"participant_profile_id": "gemini", "role": "panelist"},
                {"participant_profile_id": "mistral", "role": "panelist"},
            ],
        }
    )


def test_discussion_duration_estimate_reserves_audio_headroom() -> None:
    settings = Settings(words_per_second=2.0, discussion_duration_audio_safety_factor=0.8)
    engine = DiscussionEngine(ModelGateway(), settings)

    assert engine._estimate_duration("one two three four") == 2.5


def test_opening_instruction_uses_episode_scoped_brief_sources_and_cast() -> None:
    payload = definition().model_dump(mode="json")
    payload["media"]["opening"] = {
        "enabled": True,
        "narration_brief": "Explain why AI-assisted coding is under scrutiny.",
        "source_references": ["https://example.test/report", "Industry survey"],
        "introduce_participants": True,
    }
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )

    instruction = DiscussionEngine(ModelGateway(), Settings())._opening_instruction(episode)

    assert "evidence-led topic primer" in instruction
    assert "AI-assisted coding is under scrutiny" in instruction
    assert "https://example.test/report" in instruction
    assert "Optimist" in instruction
    assert "Host" not in instruction


def test_post_primer_bridge_contract_only_introduces_panel_when_enabled() -> None:
    payload = definition().model_dump(mode="json")
    payload["media"]["opening"] = {
        "enabled": True,
        "introduce_participants": False,
    }
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())

    bridge = engine._turn_contract(
        episode,
        episode.discussion_session or DiscussionSession(episode_id=episode.id),
        default_participants()[0],
        TurnType.post_primer_bridge,
        "post_primer_bridge",
        {},
        30,
    )

    assert "first live studio voice" in bridge["contribution_instruction"]
    assert "Do not quote it, restate its facts" in bridge["contribution_instruction"]
    assert "Use exactly four short spoken sentences" not in bridge["contribution_instruction"]


def test_post_primer_bridge_uses_explicit_episode_configuration() -> None:
    payload = definition().model_dump(mode="json")
    payload["media"]["opening"] = {
        "enabled": True,
        "post_primer_bridge": {
            "editorial_brief": "Frame the disagreement as a practical trade-off.",
            "target_duration_seconds": 30,
            "introduce_participants": True,
        },
    }
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())

    bridge = engine._turn_contract(
        episode,
        DiscussionSession(episode_id=episode.id),
        default_participants()[0],
        TurnType.post_primer_bridge,
        "post_primer_bridge",
        {},
        30,
    )

    assert engine._initial_host_turn_type(episode) == TurnType.post_primer_bridge
    assert engine._post_primer_bridge_duration_allowance(episode, 40) == 30
    assert "about 30 seconds" in bridge["contribution_instruction"]
    assert "Frame the disagreement as a practical trade-off." in bridge["contribution_instruction"]
    assert "Use exactly four short spoken sentences" in bridge["contribution_instruction"]
    assert "Name every guest in the locked roster exactly once" in bridge[
        "contribution_instruction"
    ]

    no_primer = episode.model_copy(deep=True)
    no_primer.definition.media.opening.enabled = False
    assert engine._initial_host_turn_type(no_primer) == TurnType.host_opening


def test_post_primer_bridge_expands_legacy_duration_for_guest_biographies() -> None:
    payload = definition().model_dump(mode="json")
    payload["media"]["opening"] = {
        "enabled": True,
        "post_primer_bridge": {
            "target_duration_seconds": 20,
            "introduce_participants": True,
        },
    }
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )

    assert DiscussionEngine(ModelGateway(), Settings())._post_primer_bridge_duration_allowance(
        episode, 45
    ) == 27


@pytest.mark.asyncio
async def test_post_primer_bridge_regeneration_uses_primer_continuity_and_locked_cast() -> None:
    payload = definition().model_dump(mode="json")
    payload["media"]["opening"] = {
        "enabled": True,
        "introduce_participants": True,
        "post_primer_bridge": {
            "target_duration_seconds": 35,
            "introduce_participants": True,
        },
    }
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.workflow_control["primer_production"] = {
        "status": "completed",
        "script": (
            "AI infrastructure is growing quickly. "
            "What conditions make that growth publicly acceptable?"
        ),
    }
    gateway = QuestionRoutingGateway()
    engine = DiscussionEngine(gateway, Settings())

    produced = await engine.run(episode)

    bridge = produced.discussion_session.turns[0]
    assert bridge.turn_type == TurnType.post_primer_bridge
    initial_instruction = gateway.instructions_by_participant[0][1]
    assert "Create an evidence-led topic primer" not in initial_instruction
    assert "What conditions make that growth publicly acceptable?" in initial_instruction
    assert "The Optimist:" in initial_instruction
    assert "The Skeptic:" in initial_instruction
    assert "The Practitioner:" in initial_instruction
    assert "only people who may be introduced or addressed by name" in initial_instruction
    assert "Use exactly four short spoken sentences" in initial_instruction
    assert "technology adoption can improve work when governed well" in initial_instruction

    regenerated = await engine.regenerate_turn(produced, bridge.id, user_id="tester")

    regenerated_instruction = gateway.instructions_by_participant[-1][1]
    assert (
        "Regenerate this turn in response to the discussion so far."
        not in regenerated_instruction
    )
    assert "What conditions make that growth publicly acceptable?" in regenerated_instruction
    assert "The Optimist:" in regenerated_instruction
    assert "The Skeptic:" in regenerated_instruction
    assert "The Practitioner:" in regenerated_instruction
    assert regenerated.discussion_session.turns[0].generation_metadata["turn_contract"][
        "turn_type"
    ] == "post_primer_bridge"


@pytest.mark.asyncio
async def test_retrofitted_post_primer_bridge_creates_reviewable_revision() -> None:
    payload = definition().model_dump(mode="json")
    payload["media"]["opening"] = {"enabled": False}
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())
    produced = await engine.run(episode)
    assert produced.discussion_session is not None
    assert produced.discussion_session.turns[0].turn_type == TurnType.host_opening
    original_transcript_id = produced.canonical_transcript_version_id
    produced.definition.media.opening.enabled = True
    pending_render_approval = Approval(
        episode_id=produced.id,
        stage="preview_render_review",
        decision="pending",
    )
    produced.approvals.append(pending_render_approval)

    updated = engine.add_post_primer_bridge_draft(
        produced,
        user_id="tester",
    )

    assert updated.discussion_session.turns[0].turn_type == TurnType.post_primer_bridge
    assert updated.canonical_transcript_version_id != original_transcript_id
    canonical = next(
        transcript
        for transcript in updated.transcripts
        if transcript.id == updated.canonical_transcript_version_id
    )
    assert canonical.parent_version_id == original_transcript_id
    assert canonical.turns[0].turn_type == TurnType.post_primer_bridge
    assert updated.status == EpisodeStatus.transcript_review
    assert pending_render_approval.decision == "rejected"
    assert any(
        approval.stage == "transcript_review" and approval.decision == "pending"
        for approval in updated.approvals
    )


class LeakyModelGateway:
    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        structured = StructuredTurnOutput(
            spoken_text=f"{participant.display_name} gives a concise test response.",
            intent="opening",
            claims=[],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": "Test response.",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={
                "content": structured.model_dump(),
                "accessToken": "leaked-discussion-token",
                "nested": {"clientSecret": "leaked-discussion-secret"},
            },
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


class InventedEvidenceRefGateway:
    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        structured = StructuredTurnOutput(
            spoken_text=f"{participant.display_name} makes a concise sourced-sounding claim.",
            intent="opening",
            claims=[
                {
                    "text": "This model claim sounds factual but has an invented source ref.",
                    "claim_type": "supported",
                    "confidence": 0.8,
                    "evidence_refs": ["invented-source"],
                }
            ],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": "Claim made.",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={"content": structured.model_dump()},
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


class MixedEvidenceRefGateway:
    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        structured = StructuredTurnOutput(
            spoken_text=f"{participant.display_name} cites one real and one invented source.",
            intent="opening",
            claims=[
                {
                    "text": "This claim should retain only allowed evidence references.",
                    "claim_type": "supported",
                    "confidence": 0.8,
                    "evidence_refs": ["source-a", "invented-source"],
                }
            ],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": "Mixed refs.",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={"content": structured.model_dump()},
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


class QuestionRoutingGateway:
    def __init__(self) -> None:
        self.seen_contexts: list[tuple[str, str, list[str]]] = []
        self.instructions_by_participant: list[tuple[str, str]] = []

    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        self.seen_contexts.append(
            (
                participant.id,
                context.private_memory.participant_id,
                list(context.public_transcript),
            )
        )
        self.instructions_by_participant.append(
            (participant.id, context.latest_host_instruction)
        )
        questions_for_others = []
        if participant.id == "optimist":
            questions_for_others.append(
                {
                    "participant_id": "skeptic",
                    "question": "What evidence would change your risk assessment?",
                }
        )
        structured = StructuredTurnOutput(
            spoken_text=(
                f"{participant.display_name} addresses {context.phase} with a direct point."
            ),
            intent="rebuttal" if participant.participant_type != "host" else "question",
            claims=[],
            questions_for_others=questions_for_others,
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [f"{participant.id} follow-up"],
                "open_questions": [],
                "position_summary": f"{participant.id} position",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={"content": structured.model_dump()},
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


class ToolObservingGateway:
    def __init__(self) -> None:
        self.tool_results_by_participant: dict[str, list[dict]] = {}

    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        self.tool_results_by_participant[participant.id] = list(context.tool_results)
        evidence_refs = []
        if context.tool_results:
            evidence_refs = context.tool_results[0].get("evidence_refs", [])
        structured = StructuredTurnOutput(
            spoken_text=(
                f"{participant.display_name} responds with "
                f"{len(context.tool_results)} permitted tool results."
            ),
            intent="opening",
            claims=[
                {
                    "text": "Evidence-grounded tool results are available for configured speakers.",
                    "claim_type": "supported" if evidence_refs else "opinion",
                    "confidence": 0.8,
                    "evidence_refs": evidence_refs[:1],
                }
            ],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": "Tool results observed.",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={"content": structured.model_dump()},
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


class CoverageGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        dimensions = context.required_dimensions or ["general"]
        dimension = dimensions[self.calls % len(dimensions)]
        self.calls += 1
        structured = StructuredTurnOutput(
            spoken_text=f"{participant.display_name} covers {dimension} in this turn.",
            intent="question" if participant.participant_type == "host" else "rebuttal",
            claims=[
                {
                    "text": f"This claim explicitly addresses {dimension}.",
                    "claim_type": "opinion",
                    "confidence": 0.7,
                    "evidence_refs": [],
                }
            ],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": f"{participant.id} covered {dimension}",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={"content": structured.model_dump()},
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


class CoverageGuidanceGateway:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.covered: set[str] = set()

    async def generate_turn(self, endpoint, participant, context) -> ModelResponse:
        self.instructions.append(context.latest_host_instruction)
        uncovered = [
            dimension
            for dimension in context.required_dimensions
            if dimension not in self.covered
            and dimension in context.latest_host_instruction
        ]
        dimension = uncovered[0] if uncovered else "general"
        if dimension != "general":
            self.covered.add(dimension)
        structured = StructuredTurnOutput(
            spoken_text=f"{participant.display_name} addresses {dimension}.",
            intent="question" if participant.participant_type == "host" else "rebuttal",
            claims=[
                {
                    "text": f"This turn explicitly discusses {dimension}.",
                    "claim_type": "opinion",
                    "confidence": 0.7,
                    "evidence_refs": [],
                }
            ],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": f"{participant.id} addressed {dimension}",
            },
        )
        return ModelResponse(
            structured=structured,
            raw={"content": structured.model_dump()},
            metadata={"provider_type": endpoint.provider_type, "model_id": participant.model_id},
        )


@pytest.mark.asyncio
async def test_discussion_runs_turn_by_turn_and_creates_reviewable_transcripts() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())

    produced = await engine.run(episode)

    assert produced.status == EpisodeStatus.transcript_review
    assert produced.discussion_session is not None
    session = produced.discussion_session
    assert len(produced.discussion_session.turns) >= 10
    assert produced.discussion_session.turns[0].speaker_participant_id == "host"
    assert produced.discussion_session.turns[0].turn_type == TurnType.post_primer_bridge
    assert all(turn.discussion_session_id == session.id for turn in session.turns)
    assert set(session.memories) == {participant.id for participant in produced.participants}
    assert all(memory.discussion_session_id == session.id for memory in session.memories.values())
    assert len({memory.id for memory in session.memories.values()}) == len(session.memories)
    assert {transcript.type for transcript in produced.transcripts} == {
        TranscriptType.raw,
        TranscriptType.broadcast,
    }
    broadcast = next(t for t in produced.transcripts if t.type == TranscriptType.broadcast)
    assert broadcast.parent_version_id is not None
    assert len(broadcast.turns) == len(produced.discussion_session.turns)
    assert all(turn.transcript_version_id == broadcast.id for turn in broadcast.turns)
    assert broadcast.turns[0].turn_type == TurnType.post_primer_bridge
    assert all(turn.source_discussion_turn_ids for turn in broadcast.turns)
    assert produced.approvals[0].stage == "transcript_review"
    assert produced.quality_results[0].status == "pass"
    assert all(produced.discussion_session.coverage_state.values())
    assert all(
        "coverage" in turn.generation_metadata for turn in produced.discussion_session.turns
    )
    structure_qc = next(
        result
        for result in produced.quality_results
        if result.check_type == "discussion_minimum_structure"
    )
    assert structure_qc.details["missing_dimensions"] == []
    assert structure_qc.details["covered_dimension_count"] == len(
        produced.definition.topic.required_dimensions
    )


@pytest.mark.asyncio
async def test_discussion_applies_episode_roles_and_represents_frontier_cast() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=frontier_cast_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(CoverageGateway(), Settings())

    produced = await engine.run(episode)

    session = produced.discussion_session
    assert session is not None
    expected_cast = {"claude", "chatgpt", "deepseek", "grok", "gemini", "mistral"}
    participant_type_by_id = {
        participant.id: participant.participant_type for participant in produced.participants
    }
    assert {participant.id for participant in produced.participants} == expected_cast
    assert participant_type_by_id["claude"] == "host"
    assert participant_type_by_id["chatgpt"] == "panelist"
    assert session.turns[0].speaker_participant_id == "claude"
    represented_speaker_ids = {
        turn.speaker_participant_id for turn in session.turns if turn.status != "excluded"
    }
    assert represented_speaker_ids == expected_cast
    structure_qc = next(
        result
        for result in produced.quality_results
        if result.check_type == "discussion_minimum_structure"
    )
    assert structure_qc.details["represented_speaker_ids"] == sorted(expected_cast)
    assert structure_qc.details["missing_speaker_ids"] == []


@pytest.mark.asyncio
async def test_discussion_controller_instructs_turns_to_cover_missing_dimensions() -> None:
    repo = EpisodeRepository()
    gateway = CoverageGuidanceGateway()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(gateway, Settings())

    produced = await engine.run(episode)

    session = produced.discussion_session
    assert session is not None
    assert all(session.coverage_state.values())
    assert any("still-uncovered discussion dimension" in item for item in gateway.instructions)
    assert all(
        "coverage_guidance" in turn.generation_metadata
        for turn in produced.discussion_session.turns
    )
    structure_qc = next(
        result
        for result in produced.quality_results
        if result.check_type == "discussion_minimum_structure"
    )
    assert structure_qc.status == "pass"
    assert structure_qc.details["missing_dimensions"] == []


@pytest.mark.asyncio
async def test_discussion_tracks_required_dimension_coverage_and_refreshes_on_edit() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(CoverageGateway(), Settings())

    produced = await engine.run(episode)

    session = produced.discussion_session
    assert session is not None
    assert session.coverage_state == {
        "productivity": True,
        "employment": True,
        "quality": True,
    }
    assert session.turns[0].generation_metadata["coverage"]["schema_version"] == (
        "discussion_turn_coverage.v1"
    )
    assert session.turns[0].generation_metadata["coverage"]["covered_dimensions"] == [
        "productivity"
    ]

    quality_turns = [
        turn
        for turn in session.turns
        if "quality" in turn.generation_metadata["coverage"]["covered_dimensions"]
    ]
    refreshed = produced
    for quality_turn in quality_turns:
        refreshed = engine.exclude_turn(
            refreshed,
            quality_turn.id,
            user_id="tester",
            comment="remove quality coverage turns",
        )

    refreshed_session = refreshed.discussion_session
    assert refreshed_session is not None
    assert refreshed_session.coverage_state["quality"] is False
    structure_qc = [
        result
        for result in refreshed.quality_results
        if result.check_type == "discussion_minimum_structure"
    ][-1]
    assert structure_qc.status == "fail"
    assert structure_qc.details["missing_dimensions"] == ["quality"]
    semantic_qc = [
        result
        for result in refreshed.quality_results
        if result.check_type == "transcript_semantic_fidelity"
    ][-1]
    assert semantic_qc.details["warning_count"] >= 1


def test_discussion_coverage_matches_german_umlaut_dimension_terms() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=german_dimension_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(CoverageGateway(), Settings())
    episode.discussion_session = engine._new_session(episode)
    assert episode.discussion_session is not None
    turn = DiscussionTurn(
        discussion_session_id=episode.discussion_session.id,
        sequence_number=1,
        speaker_participant_id="host",
        turn_type="opening_position",
        spoken_text=(
            "Die Stärken liegen im Reasoning, die Schwächen in Kosten und Latenz. "
            "Das Kosten-Nutzen-Verhaeltnis passt, daher lautet die "
            "Einsatzempfehlung: gezielt fuer komplexe Analyse einsetzen."
        ),
        intent="test",
        estimated_duration_seconds=8,
        structured_output=StructuredTurnOutput(
            spoken_text="Test response.",
            intent="test",
            claims=[],
            questions_for_others=[],
            requested_follow_up=False,
            private_memory_update={
                "unspoken_points": [],
                "open_questions": [],
                "position_summary": "test",
            },
        ),
        raw_provider_response={},
        generation_metadata={},
    )

    covered = engine._update_coverage_state(episode, episode.discussion_session, turn)

    assert covered == [
        "staerken",
        "schwaechen",
        "kosten_nutzen",
        "einsatzempfehlung",
    ]
    assert all(episode.discussion_session.coverage_state.values())


@pytest.mark.asyncio
async def test_discussion_ignores_disabled_participant_profiles() -> None:
    repo = EpisodeRepository()
    participants = default_participants()
    disabled = participants[1].model_copy(
        update={
            "id": "disabled-observer",
            "name": "disabled-observer",
            "display_name": "Disabled Observer",
            "enabled": False,
        }
    )
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=[
                *participants,
                disabled,
            ],
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.participants.append(disabled)
    engine = DiscussionEngine(QuestionRoutingGateway(), Settings())

    produced = await engine.run(episode)

    assert produced.discussion_session is not None
    assert "disabled-observer" not in produced.discussion_session.memories
    assert "disabled-observer" not in produced.discussion_session.speaker_balance_state
    assert all(
        turn.speaker_participant_id != "disabled-observer"
        for turn in produced.discussion_session.turns
    )


@pytest.mark.asyncio
async def test_discussion_rejects_rejected_required_research_review() -> None:
    payload = definition().model_dump(mode="json")
    payload["research"] = {
        "enabled": True,
        "approval_required": True,
        "require_source_links": True,
        "depth": "standard",
    }
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.evidence_pack,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            status="completed",
            generation_metadata={
                "evidence_pack": {
                    "source_index": [],
                    "verified_facts": [],
                    "supported_claims": [],
                    "uncertain_claims": [],
                    "disputed_claims": [],
                }
            },
        )
    )
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="research_review",
            decision="rejected",
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())

    with pytest.raises(ValueError, match="rejected research approval blocks discussion"):
        await engine.run(episode)


@pytest.mark.asyncio
async def test_discussion_persists_redacted_raw_provider_responses() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=short_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(LeakyModelGateway(), Settings())

    produced = await engine.run(episode)
    turn = produced.discussion_session.turns[0]

    assert turn.raw_provider_response["accessToken"] == "[redacted]"
    assert turn.raw_provider_response["nested"]["clientSecret"] == "[redacted]"
    raw_json = json.dumps(turn.raw_provider_response, sort_keys=True)
    assert "leaked-discussion-token" not in raw_json
    assert "leaked-discussion-secret" not in raw_json

    regenerated = await engine.regenerate_turn(produced, turn.id, user_id="tester")
    regenerated_turn = regenerated.discussion_session.turns[0]
    history = regenerated_turn.generation_metadata["regeneration_history"][0]
    assert regenerated_turn.raw_provider_response["accessToken"] == "[redacted]"
    assert history["previous_raw_provider_response"]["accessToken"] == "[redacted]"
    history_json = json.dumps(history, sort_keys=True)
    assert "leaked-discussion-token" not in history_json
    assert "leaked-discussion-secret" not in history_json


@pytest.mark.asyncio
async def test_discussion_strips_invented_evidence_refs_without_research_pack() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=short_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(InventedEvidenceRefGateway(), Settings())

    produced = await engine.run(episode)

    assert produced.discussion_session is not None
    turn = produced.discussion_session.turns[0]
    claim = turn.structured_output.claims[0]
    assert claim.evidence_refs == []
    assert claim.claim_type == "opinion"
    assert turn.generation_metadata["citation_ref_sanitization"] == {
        "schema_version": "discussion_citation_ref_sanitization.v1",
        "policy": "only_episode_evidence_pack_source_ids_may_be_cited",
        "allowed_evidence_ref_count": 0,
        "stripped_evidence_refs": ["invented-source"],
        "stripped_evidence_ref_count": 1,
        "downgraded_claim_count": 1,
    }
    assert any(
        event.event_type == "discussion.citation_refs.sanitized"
        and event.details["turn_id"] == str(turn.id)
        for event in produced.audit_events
    )


@pytest.mark.asyncio
async def test_discussion_keeps_only_evidence_pack_source_refs() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=short_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.evidence_pack,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            status="completed",
            generation_metadata={
                "evidence_pack": {
                    "verified_facts": [],
                    "supported_claims": [],
                    "uncertain_claims": [],
                    "disputed_claims": [],
                    "source_index": [
                        {
                            "id": "source-a",
                            "title": "Allowed source",
                            "uri": "https://example.test/source-a",
                            "source_type": "article",
                        }
                    ],
                }
            },
        )
    )
    engine = DiscussionEngine(MixedEvidenceRefGateway(), Settings())

    produced = await engine.run(episode)

    assert produced.discussion_session is not None
    turn = produced.discussion_session.turns[0]
    claim = turn.structured_output.claims[0]
    assert claim.evidence_refs == ["source-a"]
    assert claim.claim_type == "supported"
    assert turn.generation_metadata["citation_ref_sanitization"] == {
        "schema_version": "discussion_citation_ref_sanitization.v1",
        "policy": "only_episode_evidence_pack_source_ids_may_be_cited",
        "allowed_evidence_ref_count": 1,
        "stripped_evidence_refs": ["invented-source"],
        "stripped_evidence_ref_count": 1,
        "downgraded_claim_count": 0,
    }


@pytest.mark.asyncio
async def test_discussion_enforces_duration_and_monologue_limits() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=short_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())

    produced = await engine.run(episode)

    assert produced.discussion_session is not None
    assert (
        produced.discussion_session.estimated_duration_seconds
        <= episode.maximum_duration_seconds
    )
    assert all(
        turn.estimated_duration_seconds <= episode.definition.format.maximum_monologue_seconds
        for turn in produced.discussion_session.turns
    )
    assert any(turn.status == "duration_adjusted" for turn in produced.discussion_session.turns)
    duration_qc = [
        result
        for result in produced.quality_results
        if result.check_type == "discussion_duration_control"
    ][-1]
    assert duration_qc.status in {"pass", "warning"}
    assert duration_qc.details["failure_count"] == 0


@pytest.mark.asyncio
async def test_discussion_controller_routes_requested_responses_and_records_scores() -> None:
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    gateway = QuestionRoutingGateway()
    engine = DiscussionEngine(gateway, Settings())

    produced = await engine.run(episode)

    session = produced.discussion_session
    assert session is not None
    assert all(
        participant_id == memory_participant_id
        for participant_id, memory_participant_id, _ in gateway.seen_contexts
    )
    first_question_index = next(
        index
        for index, turn in enumerate(session.turns)
        if turn.turn_type.value == "question" and turn.sequence_number > 1
    )
    first_dynamic_turn = session.turns[first_question_index + 1]
    assert first_dynamic_turn.speaker_participant_id == "skeptic"
    selection = first_dynamic_turn.generation_metadata["speaker_selection"]
    assert selection["schema_version"] == "speaker_selection.v1"
    assert selection["policy"] == "deterministic_discussion_controller_v1"
    assert selection["selected_participant_id"] == "skeptic"
    skeptic_score = next(
        item for item in selection["candidate_scores"] if item["participant_id"] == "skeptic"
    )
    assert skeptic_score["score_components"]["requested_response_weight"] == 4.0
    assert selection["addressed_question_ids"]
    skeptic_instruction = next(
        instruction
        for participant_id, instruction in gateway.instructions_by_participant
        if participant_id == "skeptic"
        and "You were selected to answer this direct question" in instruction
    )
    assert "What evidence would change your risk assessment?" in skeptic_instruction
    assert "Turn contract:" in skeptic_instruction
    assert "episode source language (en)" in skeptic_instruction
    first_closing_turn = next(
        turn for turn in session.turns if turn.turn_type.value == "closing_statement"
    )
    assert first_closing_turn.generation_metadata["speaker_selection"][
        "minimum_remaining_turns_after_selection"
    ] == 3
    assert session.controller_state["answered_question_count"] >= 1
    assert session.controller_state["last_selection_policy"] == (
        "deterministic_discussion_controller_v1"
    )


@pytest.mark.asyncio
async def test_high_intensity_adds_linked_cross_examination_turns() -> None:
    payload = definition().model_dump(mode="json")
    payload["format"]["discussion_intensity"] = "high"
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    gateway = QuestionRoutingGateway()

    produced = await DiscussionEngine(gateway, Settings()).run(episode)

    session = produced.discussion_session
    assert session is not None
    cross_examination_turns = [
        turn
        for turn in session.turns
        if turn.generation_metadata["interaction"]["mode"] == "cross_examination"
    ]
    assert len(cross_examination_turns) == 2
    assert all(turn.responding_to_turn_id is not None for turn in cross_examination_turns)
    assert all(
        turn.generation_metadata["interaction"]["target_participant_ids"]
        for turn in cross_examination_turns
    )
    assert any(
        "one to four short, natural spoken sentences" in instruction
        for _, instruction in gateway.instructions_by_participant
    )
    conversation_qc = next(
        result
        for result in produced.quality_results
        if result.check_type == "discussion_conversation_quality"
    )
    assert conversation_qc.details["discussion_intensity"] == "high"
    assert conversation_qc.details["cross_examination_turn_count"] == 2


@pytest.mark.asyncio
async def test_medium_intensity_keeps_the_standard_discussion_shape() -> None:
    payload = definition().model_dump(mode="json")
    payload["format"]["discussion_intensity"] = "medium"
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=EpisodeDefinition.model_validate(payload),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )

    produced = await DiscussionEngine(QuestionRoutingGateway(), Settings()).run(episode)

    assert produced.discussion_session is not None
    assert not any(
        turn.generation_metadata["interaction"]["mode"] == "cross_examination"
        for turn in produced.discussion_session.turns
    )


def test_discussion_reserves_time_for_closing_positions_and_host_synthesis() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=frontier_cast_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())
    session = engine._new_session(episode)

    remaining_turns = engine._minimum_remaining_cast_turns_after_selection(
        episode.participants,
        session,
        "claude",
    )

    # Five opening positions, a host question, five discussion responses, a
    # focused host challenge, five closing positions, and a host synthesis.
    assert remaining_turns == 18

    high_intensity_remaining_turns = engine._minimum_remaining_cast_turns_after_selection(
        episode.participants,
        session,
        "claude",
        discussion_intensity="high",
    )

    assert high_intensity_remaining_turns == 20


def test_final_turn_duration_allowance_still_respects_monologue_cap() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=frontier_cast_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    engine = DiscussionEngine(ModelGateway(), Settings())
    session = engine._new_session(episode)

    allowance = engine._turn_duration_allowance(episode, session, remaining_turns=1)

    assert allowance == episode.definition.format.maximum_monologue_seconds


def test_controller_routes_panel_wide_questions_to_an_eligible_speaker() -> None:
    engine = DiscussionEngine(ModelGateway(), Settings())

    addressed = engine._questions_addressed_to(
        [
            {"participant_id": "panel", "question": "What is the first decision?"},
            {"participant_id": "other", "question": "Not for this participant."},
        ],
        "skeptic",
    )

    assert addressed == [{"participant_id": "panel", "question": "What is the first decision?"}]


def test_duration_control_prefers_a_complete_sentence_boundary() -> None:
    engine = DiscussionEngine(ModelGateway(), Settings())
    structured = StructuredTurnOutput(
        spoken_text="One two three four. Five six seven eight nine ten eleven twelve.",
        intent="test",
        claims=[],
        questions_for_others=[],
        requested_follow_up=False,
        private_memory_update={
            "unspoken_points": [],
            "open_questions": [],
            "position_summary": "test",
        },
    )

    adjusted, metadata, status = engine._apply_duration_controls(
        structured,
        allowed_seconds=4,
        maximum_monologue_seconds=10,
    )

    assert status == "duration_adjusted"
    assert adjusted.spoken_text == "One two three four."
    assert metadata["duration_control"]["truncation_strategy"] == "complete_sentence_boundary"


@pytest.mark.asyncio
async def test_discussion_uses_configured_evidence_lookup_tool_and_logs_usage() -> None:
    repo = EpisodeRepository()
    participants = [
        participant.model_copy(update={"tool_policy_id": "evidence_pack_lookup"})
        if participant.id == "optimist"
        else participant
        for participant in default_participants()
    ]
    episode = repo.create(
        EpisodeCreateRequest(
            definition=short_definition(),
            participants=participants,
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.evidence_pack,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            status="completed",
            generation_metadata={
                "evidence_pack": {
                    "verified_facts": [
                        {
                            "text": (
                                "AI coding assistants can improve software development "
                                "productivity when teams keep review and testing discipline."
                            ),
                            "claim_type": "supported",
                            "confidence": 0.82,
                            "evidence_refs": ["source-a"],
                        }
                    ],
                    "supported_claims": [
                        {
                            "text": (
                                "Quality controls remain important when AI changes "
                                "professional software development."
                            ),
                            "claim_type": "supported",
                            "confidence": 0.76,
                            "evidence_refs": ["source-a"],
                        }
                    ],
                    "uncertain_claims": [],
                    "disputed_claims": [],
                    "source_index": [
                        {
                            "id": "source-a",
                            "title": "Controlled study",
                            "uri": "https://example.test/study",
                            "source_type": "academic_paper",
                        }
                    ],
                }
            },
        )
    )
    gateway = ToolObservingGateway()
    engine = DiscussionEngine(gateway, Settings())

    produced = await engine.run(episode)

    assert gateway.tool_results_by_participant["optimist"]
    assert gateway.tool_results_by_participant["optimist"][0]["tool_name"] == (
        "evidence_pack_lookup"
    )
    assert gateway.tool_results_by_participant["optimist"][0]["evidence_refs"] == ["source-a"]
    assert gateway.tool_results_by_participant["host"] == []
    assert gateway.tool_results_by_participant["skeptic"] == []

    session = produced.discussion_session
    assert session is not None
    optimist_turn = next(
        turn for turn in session.turns if turn.speaker_participant_id == "optimist"
    )
    tool_usage = optimist_turn.generation_metadata["tool_usage"]
    assert tool_usage["schema_version"] == "discussion_tool_usage.v1"
    assert tool_usage["policy_id"] == "evidence_pack_lookup"
    assert tool_usage["tool_call_count"] == 1
    assert tool_usage["result_count"] >= 1
    assert tool_usage["calls"][0]["tool_name"] == "evidence_pack_lookup"
    assert session.controller_state["tool_call_count"] >= 1
    assert session.controller_state["tool_result_count"] >= 1
    assert session.controller_state["tool_usage_log"]
    assert any(
        event.event_type == "discussion.tools.used"
        and event.details["speaker_participant_id"] == "optimist"
        for event in produced.audit_events
    )
