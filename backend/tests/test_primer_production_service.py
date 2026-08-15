import asyncio
import copy
import hashlib
from types import SimpleNamespace

import pytest
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.enums import AssetType
from app.domain.schemas import (
    Asset,
    EpisodeCreateRequest,
    PrimerNarratorProfile,
    PrimerProductionRequest,
    PrimerPronunciationSettings,
    PrimerSpokenScriptApprovalRequest,
    PrimerSpokenScriptPrepareRequest,
    PrimerSpokenScriptReplacement,
    PrimerSpokenScriptUpdateRequest,
    PrimerVisualPlanBeatCreateRequest,
    PrimerVisualPlanBeatUpdateRequest,
    PrimerVisualPlanVerificationRequest,
    PronunciationDictionaryEntry,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.object_storage import create_object_store
from app.services.primer_production_service import PrimerProductionService
from app.services.render_service import RenderService


def test_deterministic_primer_script_matches_requested_voiceover_duration() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    service.settings = Settings(words_per_second=2.45)
    episode = SimpleNamespace(
        definition=SimpleNamespace(
            topic=SimpleNamespace(
                central_question=(
                    "Wie kann der Ausbau von KI-Rechenzentren verantwortbar gestaltet werden?"
                ),
                required_dimensions=["Energie", "Wasser", "Wirtschaft"],
            )
        )
    )

    script = service._deterministic_script(
        episode,
        [{"title": "Amtliche Analyse"}, {"title": "Netzbericht"}],
        target_seconds=60,
    )

    assert len(script.split()) == 147
    assert "Wie kann der Ausbau" in script
    assert "Amtliche Analyse" in script
    assert script.endswith("tragfaehig bleibt?")


def test_primer_evidence_excludes_internal_episode_configuration() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = SimpleNamespace(
        assets=[
            SimpleNamespace(
                asset_type=AssetType.evidence_pack,
                status="completed",
                generation_metadata={
                    "evidence_pack": {
                        "source_index": [
                            {
                                "title": "Episode definition",
                                "source_type": "episode_configuration",
                                "uri": "dialecticore://episodes/test/definition",
                            },
                            {
                                "title": "Official source",
                                "source_type": "government_report",
                                "uri": "https://government.example/report",
                            },
                        ]
                    }
                },
            )
        ]
    )

    assert service._evidence_sources(episode) == [
        {
            "title": "Official source",
            "source_type": "government_report",
            "uri": "https://government.example/report",
        }
    ]


def test_primer_editorial_validation_allows_flexible_pacing_and_rejects_repetition() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    fluent = " ".join(
        [
            "KI-Rechenzentren sind laengst mehr als technische Anlagen.",
            (
                "Ihr Ausbau entscheidet mit darueber, wie viel Strom und Wasser vor Ort gebraucht "
                "werden und wer davon profitiert."
            ),
            (
                "Gleichzeitig versprechen sie neue Kapazitaeten fuer Forschung, Verwaltung und "
                "Unternehmen."
            ),
            (
                "Sie koennen regionale Infrastruktur staerken, aber auch neue Konflikte um "
                "Ressourcen ausloesen."
            ),
            (
                "Die offene Frage ist deshalb nicht nur, wie schnell gebaut wird, sondern unter "
                "welchen Bedingungen dieser Ausbau akzeptiert und dauerhaft tragfaehig bleibt?"
            ),
        ]
    )
    repeated = " ".join(["Der gleiche Satz wird ohne neuen Gedanken wiederholt."] * 12) + "?"

    assert service._is_usable_polished_script(fluent, target_words=80)
    assert not service._is_usable_polished_script(repeated, target_words=80)
    assert not service._is_reusable_editorial_script(
        fluent,
        {"status": "fallback"},
        target_words=80,
    )
    assert service._is_reusable_editorial_script(
        fluent,
        {"status": "applied"},
        target_words=80,
    )


def test_primer_editorial_polish_repairs_one_invalid_response(monkeypatch) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    service.settings = Settings()
    invalid = (
        "KI-Rechenzentren brauchen Strom und Wasser. Ihr Ausbau schafft Kapazitaeten. "
        "Die Folgen werden kontrovers diskutiert. Wie soll der Ausbau weitergehen? "
        "Welche Bedingungen braucht er?"
    )
    repaired = " ".join(
        [
            (
                "KI-Rechenzentren werden zur Grundlage fuer neue Anwendungen in Forschung, "
                "Verwaltung und Unternehmen."
            ),
            (
                "Gleichzeitig entstehen vor Ort neue Anforderungen an Stromnetze, Wasser und "
                "Flaechen."
            ),
            (
                "Der Nutzen kann gross sein, doch Kosten und Risiken treffen Regionen nicht "
                "immer gleich."
            ),
            (
                "Entscheidend ist deshalb, welche Regeln den Ausbau begleiten und wer fuer die "
                "Infrastruktur aufkommt."
            ),
            "Unter welchen Bedingungen kann dieser Ausbau langfristig Akzeptanz finden?",
        ]
    )
    responses = iter([invalid, repaired])
    calls: list[dict] = []

    async def rewrite(*_args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(service, "_request_editorial_rewrite", rewrite)
    episode = SimpleNamespace(
        definition=SimpleNamespace(
            topic=SimpleNamespace(
                central_question="Wie sollen KI-Rechenzentren verantwortbar wachsen?"
            ),
            media=SimpleNamespace(opening=SimpleNamespace(narration_brief="")),
        )
    )
    narrator = SimpleNamespace(
        model_id="editor-model",
        editorial_style="Praezise und neutral.",
        sampling_settings=SimpleNamespace(temperature=0.4),
    )
    endpoint = SimpleNamespace(
        id="editor-endpoint",
        base_url="https://editor.example",
        provider_type=SimpleNamespace(value="openai_compatible"),
    )

    script, metadata = asyncio.run(
        service._polish_script(
            episode,
            narrator,
            endpoint,
            [{"title": "Official analysis", "summary": "Evidence summary."}],
            30,
            "Initial draft.",
        )
    )

    assert script == repaired
    assert metadata["status"] == "applied_after_repair"
    assert len(calls) == 2


def test_primer_production_request_supports_revoice_and_script_override() -> None:
    request = PrimerProductionRequest(
        regenerate=True,
        reuse_existing_script=True,
        regenerate_narration=True,
        script_override=(
            "Dieser zuvor gepruefte Text wird fuer eine neue Sprecherstimme erneut vertont, "
            "bleibt nachvollziehbar und endet mit einer Frage?"
        ),
    )

    assert request.regenerate is True
    assert request.reuse_existing_script is True
    assert request.regenerate_narration is True
    assert request.script_override is not None


def _pronunciation_narrator(language: str = "en") -> PrimerNarratorProfile:
    return PrimerNarratorProfile(
        id="pronunciation-narrator",
        name="Pronunciation Narrator",
        language=language,
        model_endpoint_id="editor-endpoint",
        model_id="editor-model",
        voice_profile_id="voice-profile",
        pronunciation=PrimerPronunciationSettings(
            enabled=True,
            use_ai=False,
            acronym_policy="spell_out",
            custom_dictionary=[
                PronunciationDictionaryEntry(
                    source="NVIDIA",
                    spoken="Enwidia",
                    category="name",
                )
            ],
        ),
    )


def test_spoken_script_is_reviewed_separately_and_profile_changes_make_it_stale(
    monkeypatch,
) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    editorial = (
        "Die EU und NVIDIA investieren in KI-Infrastruktur. "
        "Die Technik benötigt 20 MW Leistung. "
        "Der Ausbau steigt um 7,5 %. "
        "Öffentliche Stellen prüfen Kosten und Nutzen. "
        "Die Entscheidung bleibt an klare Bedingungen gebunden. "
        "Welche Regeln sichern eine belastbare Entwicklung?"
    )
    episode.workflow_control["primer_production"] = {
        "script": editorial,
        "editorial_polish": {"status": "applied"},
    }
    narrator = _pronunciation_narrator("de")
    monkeypatch.setattr(service, "_approved_editorial_script", lambda _episode: editorial)

    prepared = asyncio.run(
        service.prepare_spoken_script(
            episode,
            PrimerSpokenScriptPrepareRequest(user_id="editor"),
            narrator,
            [],
        )
    )

    assert prepared.status == "review_required"
    assert "E U" in prepared.spoken_script
    assert "Enwidia" in prepared.spoken_script
    assert "K I" in prepared.spoken_script
    assert "sieben Komma fünf" in prepared.spoken_script
    assert "Megawatt" in prepared.spoken_script
    assert prepared.editorial_script == editorial

    approved = service.approve_spoken_script(
        episode,
        PrimerSpokenScriptApprovalRequest(user_id="editor"),
        narrator,
    )
    assert approved.status == "approved"
    assert approved.approved_by == "editor"

    changed_narrator = narrator.model_copy(
        update={
            "pronunciation": narrator.pronunciation.model_copy(
                update={"acronym_policy": "preserve"}
            )
        }
    )
    assert service.spoken_script_status(episode, changed_narrator).status == "outdated"


def test_spoken_script_edit_rejects_untracked_word_changes(monkeypatch) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    editorial = "Die EU prüft heute eine belastbare Lösung?"
    episode.workflow_control["primer_production"] = {
        "script": editorial,
        "editorial_polish": {"status": "applied"},
    }
    narrator = _pronunciation_narrator(episode.source_language)
    monkeypatch.setattr(service, "_approved_editorial_script", lambda _episode: editorial)

    with pytest.raises(ValueError, match="cannot add, remove, or reorder"):
        service.update_spoken_script(
            episode,
            PrimerSpokenScriptUpdateRequest(
                replacements=[
                    PrimerSpokenScriptReplacement(
                        source="EU",
                        spoken="E U",
                        category="acronym",
                        origin="editor",
                    )
                ],
                punctuation_script="Die E U prüft heute eine völlig neue belastbare Lösung?",
            ),
            narrator,
        )


def test_invalid_pronunciation_ai_output_falls_back_to_validated_rules(
    monkeypatch,
) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    editorial = "Die EU prüft KI-Infrastruktur unter klaren Bedingungen?"
    episode = _episode_for_visual_plan()
    episode.workflow_control["primer_production"] = {
        "script": editorial,
        "editorial_polish": {"status": "applied"},
    }
    narrator = _pronunciation_narrator(episode.source_language).model_copy(
        update={
            "pronunciation": _pronunciation_narrator(
                episode.source_language
            ).pronunciation.model_copy(update={"use_ai": True})
        }
    )
    endpoint = SimpleNamespace(
        id=narrator.model_endpoint_id,
        enabled=True,
        base_url="https://model.example",
    )
    monkeypatch.setattr(service, "_approved_editorial_script", lambda _episode: editorial)

    async def invalid_output(*_args, **_kwargs):
        raise ValueError("model tried to add a claim")

    monkeypatch.setattr(service, "_request_pronunciation_suggestions", invalid_output)

    prepared = asyncio.run(
        service.prepare_spoken_script(
            episode,
            PrimerSpokenScriptPrepareRequest(user_id="editor"),
            narrator,
            [endpoint],
        )
    )

    assert prepared.status == "review_required"
    assert "E U" in prepared.spoken_script
    assert "K I" in prepared.spoken_script
    assert prepared.ai_assistance["status"] == "unavailable"
    assert prepared.ai_assistance["reason"] == "pronunciation_ai_output_failed_validation"


def test_enabled_pronunciation_blocks_narration_until_spoken_script_is_approved(
    monkeypatch,
) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    service.settings = Settings()
    episode = _episode_for_visual_plan()
    editorial = " ".join(["Eine belastbare Aussage für den Primer."] * 35) + "?"
    episode.workflow_control["primer_production"] = {
        "script": editorial,
        "editorial_polish": {"status": "applied"},
    }
    narrator = _pronunciation_narrator(episode.source_language)
    monkeypatch.setattr(service, "_is_reusable_editorial_script", lambda *_args: True)

    with pytest.raises(ValueError, match="prepare, review, and approve"):
        asyncio.run(
            service.prepare_narration_timing(
                episode,
                SimpleNamespace(regenerate=False, user_id="editor"),
                narrator,
                [],
                [],
            )
        )


def test_primer_rerender_forces_one_fresh_narration_and_returns_timing_review(
    monkeypatch,
) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    script = " ".join(["Eine gepruefte, fluente Primer-Erzaehlung."] * 35) + "?"
    state = {
        "script": script,
        "editorial_polish": {"status": "applied"},
    }
    visual_plan = {
        "status": "approved",
        "beats": [{"id": "beat-1"}],
        "script_checksum": hashlib.sha256(script.encode()).hexdigest(),
    }
    reuse_requests: list[bool] = []
    narrated_texts: list[str] = []
    narrator = _pronunciation_narrator(episode.source_language)
    spoken_script = script.replace("Primer", "P R I M E R")
    state["spoken_script"] = {
        "status": "approved",
        "editorial_script_checksum": hashlib.sha256(script.encode()).hexdigest(),
        "spoken_script": spoken_script,
        "spoken_script_checksum": hashlib.sha256(spoken_script.encode()).hexdigest(),
        "profile_fingerprint": service._pronunciation_profile_fingerprint(narrator),
        "replacements": [
            {
                "source": "Primer",
                "spoken": "P R I M E R",
                "category": "custom",
                "origin": "editor",
            }
        ],
    }

    monkeypatch.setattr(service, "_state", lambda _episode: state)
    monkeypatch.setattr(service, "_evidence_sources", lambda _episode: [{"title": "Source"}])
    monkeypatch.setattr(service, "_visual_plan_state", lambda _episode: visual_plan)
    monkeypatch.setattr(
        service,
        "_visual_plan_coverage",
        lambda _episode, _beats: {"ready": True, "blockers": []},
    )
    monkeypatch.setattr(service, "_asset_by_id", lambda _episode, _asset_id: None)
    monkeypatch.setattr(service, "_target_word_count", lambda _seconds: 100)
    monkeypatch.setattr(service, "_is_reusable_editorial_script", lambda *_args: True)
    monkeypatch.setattr(service, "_visual_plan_assets", lambda *_args: [])

    def reusable_narration(*_args, reuse_requested: bool | None = None):
        requested = reuse_requested if reuse_requested is not None else _args[-1]
        reuse_requests.append(bool(requested))
        return None

    async def narrate(*_args, **_kwargs):
        narrated_texts.append(_args[1])
        return Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            source_entity_type="primer_narration",
            source_entity_id="rerender-test",
            status="completed",
            mime_type="audio/wav",
            duration_ms=63_500,
            generation_metadata={"transcription_qc": {"status": "passed"}},
        )

    monkeypatch.setattr(service, "_reusable_narration_asset", reusable_narration)
    monkeypatch.setattr(service, "_narrate", narrate)
    monkeypatch.setattr(
        service,
        "_synchronise_visual_plan_to_narration",
        lambda *_args, **_kwargs: {"review_required": True},
    )
    monkeypatch.setattr(
        service,
        "status",
        lambda _episode, _narrator=None: SimpleNamespace(status=state["status"]),
    )

    result = asyncio.run(
        service.produce(
            episode,
            PrimerProductionRequest(
                regenerate=True,
                reuse_existing_script=True,
                regenerate_narration=True,
                user_id="tester",
            ),
                narrator,
            [],
            [],
            [],
        )
    )

    assert reuse_requests == [False]
    assert narrated_texts == [spoken_script]
    assert result.status == "visual_timing_review"
    assert state["narration_asset_id"] == str(episode.assets[-1].id)
    assert episode.audit_events[-1].event_type == "primer.production.timing_review_required"


def test_render_timeout_scales_for_long_source_visuals() -> None:
    assert RenderService._ffmpeg_timeout_seconds(2) == 34
    assert RenderService._ffmpeg_timeout_seconds(60) == 150
    assert RenderService._ffmpeg_timeout_seconds(180, minimum=60) == 390
    assert RenderService._ffmpeg_timeout_seconds(1000) == 600


def test_terminal_primer_clip_uses_a_source_cut_and_fades_only_at_the_end() -> None:
    service = RenderService(Settings())

    policy = service._transition_policy_for_segment(
        {
            "camera_transition": "dissolve",
            "source_range_timing_locked": True,
            "terminal_clip": True,
            "terminal_fade_out_ms": 500,
        },
        2,
    )
    filter_text = service._append_transition_video_filter(
        "format=yuv420p",
        policy,
        4.0,
    )

    assert policy["cross_scene"] is False
    assert policy["duration_ms"] == 0
    assert "fade=t=in" not in filter_text
    assert "fade=t=out:st=3.500:d=0.500" in filter_text


def test_render_supports_uploaded_webp_screenshots_and_webm_source_video() -> None:
    service = RenderService(Settings())
    episode_id = "00000000-0000-0000-0000-000000000001"
    screenshot = Asset(
        episode_id=episode_id,
        asset_type=AssetType.image,
        source_entity_type="episode_opening",
        source_entity_id="stakeholder-post",
        mime_type="image/webp",
    )
    source_video = Asset(
        episode_id=episode_id,
        asset_type=AssetType.video,
        source_entity_type="episode_opening",
        source_entity_id="news-clip",
        mime_type="video/webm",
    )

    assert service._visual_asset_supported(screenshot)
    assert service._visual_asset_supported(source_video)


def _episode_for_visual_plan():
    return EpisodeRepository().create(
        EpisodeCreateRequest(
            definition={
                "title": "Visual plan test",
                "topic": {
                    "central_question": "How should a public policy change be explained?",
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
            },
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )


def _opening_asset(episode, asset_type: AssetType, title: str) -> Asset:
    return Asset(
        episode_id=episode.id,
        asset_type=asset_type,
        source_entity_type="episode_opening",
        source_entity_id=title.lower().replace(" ", "-"),
        status="completed",
        mime_type="video/mp4" if asset_type == AssetType.video else "image/png",
        duration_ms=120_000 if asset_type == AssetType.video else None,
        generation_metadata={
            "opening_media": True,
            "source_title": title,
            "primer_visual_suitability": {
                "status": "verified",
                "people_visible": False,
            },
        },
    )


def _mark_video_beats_people_free(plan: dict) -> None:
    for beat in plan["beats"]:
        if beat["asset_type"] == AssetType.video.value:
            beat["people_free_verification"] = {
                "status": "verified",
                "people_visible": False,
            }


def test_visual_plan_accepts_a_single_reused_still_when_it_covers_the_narration() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    still = _opening_asset(episode, AssetType.image, "Official statistic")
    episode.assets.append(still)

    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["A factual sentence for the opening primer."] * 40),
        target_duration_seconds=30,
        visual_assets=[still],
    )

    assert plan["status"] == "review_required"
    assert plan["coverage"]["video_beat_count"] == 0
    assert plan["coverage"]["planned_visual_duration_ms"] >= 30_000


def test_visual_plan_uses_multiple_source_types_and_builds_timeline_from_reviewed_beats(
    tmp_path,
) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    service.object_store = create_object_store(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official briefing clip"),
        _opening_asset(episode, AssetType.video, "News report clip"),
        _opening_asset(episode, AssetType.image, "Newspaper front page"),
        _opening_asset(episode, AssetType.image, "Stakeholder post"),
    ]
    episode.assets.extend(assets)
    script = " ".join(["This is an evidence-led narration sentence."] * 80)
    plan = service._build_visual_plan(
        episode=episode,
        script=script,
        target_duration_seconds=30,
        visual_assets=assets,
    )
    _mark_video_beats_people_free(plan)
    plan["coverage"] = service._visual_plan_coverage(episode, plan["beats"])
    plan["status"] = "review_required" if plan["coverage"]["ready"] else "blocked"

    assert plan["status"] == "review_required"
    assert plan["coverage"]["distinct_video_asset_count"] == 2
    assert plan["coverage"]["video_duration_ms"] >= 8000
    assert any(beat["asset_type"] == "image" for beat in plan["beats"])

    plan["beats"][0]["source_start_ms"] = 1_500
    plan["beats"][0]["source_end_ms"] = 5_500
    timeline_asset = service._build_timeline(
        episode,
        SimpleNamespace(id="audio-id", duration_ms=24_000),
        assets,
        SimpleNamespace(language="de"),
        visual_plan=plan,
    )

    timeline = timeline_asset.generation_metadata["timeline_json"]
    assert len(timeline["segments"]) == 4
    assert all(segment["duration_ms"] >= 5_000 for segment in timeline["segments"])
    assert timeline["segments"][0]["source_start_ms"] == 1_500
    assert timeline["segments"][0]["source_end_ms"] == 5_500
    assert timeline["segments"][0]["still_motion"] == "push_in"


def test_visual_plan_spreads_reused_video_across_distinct_source_ranges() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official briefing clip"),
        _opening_asset(episode, AssetType.video, "News report clip"),
        _opening_asset(episode, AssetType.image, "Newspaper front page"),
    ]
    episode.assets.extend(assets)

    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 140),
        target_duration_seconds=60,
        visual_assets=assets,
    )
    _mark_video_beats_people_free(plan)
    plan["coverage"] = service._visual_plan_coverage(episode, plan["beats"])
    plan["status"] = "review_required" if plan["coverage"]["ready"] else "blocked"

    video_beats = [beat for beat in plan["beats"] if beat["asset_type"] == "video"]
    ranges = [
        (beat["asset_id"], beat["source_start_ms"], beat["source_end_ms"])
        for beat in video_beats
    ]
    assert plan["status"] == "review_required"
    assert len(ranges) == len(set(ranges))
    assert all(start > 0 and end - start >= 4_000 for _, start, end in ranges)
    assert plan["coverage"]["distinct_video_range_count"] == len(ranges)


def test_visual_plan_allows_a_repeated_video_excerpt_when_the_editor_selects_it() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official briefing clip"),
        _opening_asset(episode, AssetType.video, "News report clip"),
        _opening_asset(episode, AssetType.image, "Newspaper front page"),
    ]
    episode.assets.extend(assets)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )
    first_video = next(beat for beat in plan["beats"] if beat["asset_type"] == "video")
    repeated_video = next(
        beat
        for beat in plan["beats"]
        if beat is not first_video
        and beat["asset_type"] == "video"
        and beat["asset_id"] == first_video["asset_id"]
    )
    repeated_video["source_start_ms"] = first_video["source_start_ms"]
    repeated_video["source_end_ms"] = first_video["source_end_ms"]
    _mark_video_beats_people_free(plan)

    coverage = service._visual_plan_coverage(episode, plan["beats"])

    assert coverage["ready"] is True
    assert coverage["distinct_video_range_count"] < coverage["video_beat_count"]


def test_visual_plan_rejects_source_media_that_contains_people() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Data centre exterior"),
        _opening_asset(episode, AssetType.video, "Cooling infrastructure"),
        _opening_asset(episode, AssetType.image, "Official statistic"),
    ]
    episode.assets.extend(assets)
    assets[0].generation_metadata["primer_visual_suitability"] = {
        "status": "verified",
        "people_visible": True,
    }
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )

    coverage = service._visual_plan_coverage(episode, plan["beats"])

    assert coverage["ready"] is False
    assert "every visual beat must use source media verified as people-free" in coverage["blockers"]


def test_visual_plan_accepts_people_free_excerpts_from_mixed_video_sources() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "News report with mixed shots"),
        _opening_asset(episode, AssetType.video, "Official briefing with mixed shots"),
        _opening_asset(episode, AssetType.image, "Official statistic"),
    ]
    episode.assets.extend(assets)
    for asset in assets[:2]:
        asset.generation_metadata["primer_visual_suitability"] = {
            "status": "verified",
            "people_visible": True,
        }
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )
    _mark_video_beats_people_free(plan)

    coverage = service._visual_plan_coverage(episode, plan["beats"])

    assert coverage["ready"] is True
    assert coverage["people_free_assigned_beat_count"] == len(plan["beats"])


def test_visual_plan_add_and_remove_reflows_narration_and_retains_source_media() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official infrastructure sequence"),
        _opening_asset(episode, AssetType.video, "News context sequence"),
        _opening_asset(episode, AssetType.image, "Official statistic"),
    ]
    episode.assets.extend(assets)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )
    _mark_video_beats_people_free(plan)
    episode.workflow_control["primer_visual_plan"] = plan
    original_beat_count = len(plan["beats"])
    original_excerpts = [beat["narration_excerpt"] for beat in plan["beats"]]

    added = service.add_visual_plan_beat(
        episode,
        PrimerVisualPlanBeatCreateRequest(asset_id=assets[2].id, user_id="tester"),
    )

    assert added.beat_count == original_beat_count + 1
    assert added.beats[-1]["asset_id"] == str(assets[2].id)
    assert [beat["narration_excerpt"] for beat in added.beats] != original_excerpts
    assert added.status == "review_required"

    removed_beat_id = added.beats[-1]["id"]
    removed = service.remove_visual_plan_beat(episode, removed_beat_id, user_id="tester")

    assert removed.beat_count == original_beat_count
    assert all(asset.id in {item.id for item in episode.assets} for asset in assets)
    assert episode.audit_events[-1].event_type == "primer.visual_plan.beat.removed"


def test_visual_plan_assesses_new_stills_without_rebuilding_the_sequence() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    still = _opening_asset(episode, AssetType.image, "Newly imported policy graphic")
    still.generation_metadata["primer_visual_suitability"] = {
        "status": "not_assessed",
        "people_visible": None,
    }
    episode.assets.append(still)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 60),
        target_duration_seconds=30,
        visual_assets=[still],
    )
    episode.workflow_control["primer_visual_plan"] = plan

    async def assess_stills(**kwargs):
        assert kwargs["visual_assets"] == [still]
        still.generation_metadata["primer_visual_suitability"] = {
            "status": "verified",
            "people_visible": False,
        }
        return {"status": "assessed", "eligible_asset_count": 1}

    service._assess_visual_suitability = assess_stills
    status = asyncio.run(
        service.assess_visual_plan_source_media(
            episode,
            PrimerVisualPlanVerificationRequest(user_id="tester"),
            model_endpoints=[],
        )
    )

    assert status.status == "review_required"
    assert status.planner["people_free_policy"]["still_images"]["status"] == "assessed"
    assert episode.audit_events[-1].event_type == "primer.visual_sources.assessed"


def test_scene_detected_windows_are_bounded_and_distributed() -> None:
    windows = PrimerProductionService._scene_video_windows_from_cut_points(
        asset_id="source-video",
        source_duration_ms=70_000,
        required_duration_ms=6_500,
        maximum_count=3,
        cut_points_ms=[3_000, 16_000, 31_000, 47_000, 65_000],
    )

    assert len(windows) == 3
    assert all(4_000 <= window["end_ms"] - window["start_ms"] <= 15_000 for window in windows)
    assert all(window["selection_method"] == "scene_detected" for window in windows)
    assert windows == sorted(windows, key=lambda window: window["start_ms"])


def test_video_window_suitability_parses_json_surrounded_by_model_text() -> None:
    windows = [{"window_id": "window-a", "asset_id": "asset-a"}]

    result = PrimerProductionService._parse_video_window_suitability(
        "Result follows.\n"
        '{"windows":[{"window_id":"window-a","people_visible":false}]}\n'
        "Done.",
        windows,
    )

    assert result == {
        "window-a": {
            "people_visible": False,
            "summary": "sampled source-video excerpt",
        }
    }


def test_manual_excerpt_verification_preserves_the_producer_trim() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official infrastructure sequence"),
        _opening_asset(episode, AssetType.video, "News context sequence"),
        _opening_asset(episode, AssetType.image, "Official statistic"),
    ]
    episode.assets.extend(assets)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )
    target = next(beat for beat in plan["beats"] if beat["asset_type"] == "video")
    target["source_start_ms"] = 12_000
    target["source_end_ms"] = 19_000
    for beat in plan["beats"]:
        if beat["asset_type"] == "video":
            beat["people_free_verification"] = {
                "status": "verified",
                "people_visible": False,
            }
    target["people_free_verification"] = {
        "status": "not_verified",
        "reason": "source_range_changed_requires_people_free_review",
    }
    target["timing_source"] = "pending_manual_excerpt_verification"
    episode.workflow_control["primer_visual_plan"] = plan

    pending_coverage = service._visual_plan_coverage(episode, plan["beats"])
    assert pending_coverage["pending_manual_excerpt_count"] == 1
    assert (
        "verify every manually edited video excerpt before approving its sequence timing"
        in pending_coverage["blockers"]
    )

    async def verify_windows(**kwargs):
        assert kwargs["preserve_selected_ranges"] is True
        assert kwargs["candidate_windows"] == [
            {
                "window_id": f"manual-{target['id']}",
                "beat_id": target["id"],
                "asset_id": target["asset_id"],
                "start_ms": 12_000,
                "end_ms": 19_000,
                "selection_method": "manual_trim",
            }
        ]
        target["people_free_verification"] = {
            "status": "verified",
            "people_visible": False,
        }
        return {"status": "assessed", "reason": "manual_excerpt_review"}

    service._verify_people_free_video_windows = verify_windows
    status = asyncio.run(
        service.verify_visual_plan_excerpts(
            episode,
            PrimerVisualPlanVerificationRequest(user_id="test"),
            model_endpoints=[],
        )
    )

    verified = next(beat for beat in status.beats if beat["id"] == target["id"])
    assert verified["source_start_ms"] == 12_000
    assert verified["source_end_ms"] == 19_000
    assert verified["people_free_verification"]["status"] == "verified"
    assert verified["duration_ms"] == 7_000
    assert verified["timing_source"] == "verified_manual_excerpt"
    assert status.coverage["pending_manual_excerpt_count"] == 0


def test_applying_a_video_trim_immediately_reflows_provisional_beat_timing() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official infrastructure sequence"),
        _opening_asset(episode, AssetType.video, "News context sequence"),
        _opening_asset(episode, AssetType.image, "Official statistic"),
    ]
    episode.assets.extend(assets)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )
    episode.workflow_control["primer_visual_plan"] = plan
    target_index, target = next(
        (index, beat)
        for index, beat in enumerate(plan["beats"])
        if beat["asset_type"] == AssetType.video.value
    )
    original_excerpt_word_count = len(target["narration_excerpt"].split())

    status = service.update_visual_plan_beat(
        episode,
        target["id"],
        PrimerVisualPlanBeatUpdateRequest(
            asset_id=assets[0].id,
            source_start_ms=12_000,
            source_end_ms=19_000,
            user_id="tester",
        ),
    )

    updated = status.beats[target_index]
    assert updated["duration_ms"] == 7_000
    assert updated["manual_excerpt_duration_ms"] == 7_000
    assert updated["timing_source"] == "pending_manual_excerpt_verification"
    assert len(updated["narration_excerpt"].split()) > original_excerpt_word_count
    assert status.beats[target_index + 1]["start_ms"] == updated["end_ms"]
    assert status.coverage["pending_manual_excerpt_count"] >= 1


def test_visual_plan_revision_restores_a_prior_media_assignment_and_trim() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Official infrastructure sequence"),
        _opening_asset(episode, AssetType.video, "News context sequence"),
    ]
    episode.assets.extend(assets)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 100),
        target_duration_seconds=45,
        visual_assets=assets,
    )
    _mark_video_beats_people_free(plan)
    target = next(beat for beat in plan["beats"] if beat["asset_type"] == AssetType.video.value)
    target.update(
        {
            "asset_id": str(assets[0].id),
            "source_start_ms": 12_000,
            "source_end_ms": 19_000,
            "duration_ms": 7_000,
            "timing_source": "verified_manual_excerpt",
            "review_status": "approved",
        }
    )
    plan["status"] = "approved"
    plan["coverage"] = service._visual_plan_coverage(episode, plan["beats"])
    episode.workflow_control["primer_visual_plan"] = copy.deepcopy(plan)

    service.update_visual_plan_beat(
        episode,
        target["id"],
        PrimerVisualPlanBeatUpdateRequest(
            asset_id=assets[1].id,
            source_start_ms=24_000,
            source_end_ms=30_000,
            user_id="tester",
        ),
    )

    revisions = service.visual_plan_revisions(episode)
    assert len(revisions.revisions) == 1
    revision_id = revisions.revisions[0].id
    changed = next(
        beat
        for beat in episode.workflow_control["primer_visual_plan"]["beats"]
        if beat["id"] == target["id"]
    )
    assert changed["asset_id"] == str(assets[1].id)
    assert changed["source_start_ms"] == 24_000

    restored = service.restore_visual_plan_revision(episode, revision_id, user_id="tester")
    restored_beat = next(beat for beat in restored.beats if beat["id"] == target["id"])
    assert restored_beat["asset_id"] == str(assets[0].id)
    assert restored_beat["source_start_ms"] == 12_000
    assert restored_beat["source_end_ms"] == 19_000
    assert restored_beat["timing_source"] == "verified_manual_excerpt"


def test_timeline_preserves_beat_timing_and_clips_only_the_final_visual(tmp_path) -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    service.object_store = create_object_store(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Data centre exterior"),
        _opening_asset(episode, AssetType.video, "Cooling infrastructure"),
    ]
    episode.assets.extend(assets)
    plan = service._build_visual_plan(
        episode=episode,
        script=" ".join(["Evidence-led narration"] * 120),
        target_duration_seconds=60,
        visual_assets=assets,
    )

    timeline_asset = service._build_timeline(
        episode,
        SimpleNamespace(id="audio-id", duration_ms=10_000),
        assets,
        SimpleNamespace(language="de"),
        visual_plan=plan,
    )
    segments = timeline_asset.generation_metadata["timeline_json"]["segments"]

    assert len(segments) == 2
    assert [segment["duration_ms"] for segment in segments] == [6_000, 4_000]
    assert segments[-1]["terminal_fade_out_ms"] > 0
    assert timeline_asset.generation_metadata["timeline_json"]["visual_sequence"] == {
        "planned_duration_ms": 60_000,
        "rendered_duration_ms": 10_000,
        "terminal_clip_applied": True,
    }


def test_measured_narration_timing_reflows_unedited_visual_plan() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    assets = [
        _opening_asset(episode, AssetType.video, "Data centre exterior"),
        _opening_asset(episode, AssetType.video, "Cooling infrastructure"),
    ]
    episode.assets.extend(assets)
    script = " ".join(["Evidence-led narration."] * 120)
    episode.workflow_control["primer_production"] = {
        "script": script,
        "editorial_polish": {"status": "applied"},
    }
    plan = service._build_visual_plan(
        episode=episode,
        script=script,
        target_duration_seconds=60,
        visual_assets=assets,
    )
    _mark_video_beats_people_free(plan)
    episode.workflow_control["primer_visual_plan"] = plan
    narration = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        source_entity_type="primer_narration",
        source_entity_id="timing-track",
        status="completed",
        duration_ms=44_270,
    )

    timing = service._synchronise_visual_plan_to_narration(
        episode,
        narration,
        script,
        actor="test",
    )
    beats = episode.workflow_control["primer_visual_plan"]["beats"]

    assert timing["duration_ms"] == 44_270
    assert timing["visual_timing_reflowed"] is True
    assert timing["review_required"] is False
    assert sum(beat["duration_ms"] for beat in beats) == 44_270
    assert beats[-1]["end_ms"] == 44_270
    assert (
        episode.workflow_control["primer_visual_plan"]["coverage"][
            "target_narration_duration_ms"
        ]
        == 44_270
    )


def test_measured_narration_timing_requires_manual_clip_review() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    asset = _opening_asset(episode, AssetType.video, "Official footage")
    episode.assets.append(asset)
    script = " ".join(["Evidence-led narration."] * 120)
    episode.workflow_control["primer_production"] = {
        "script": script,
        "editorial_polish": {"status": "applied"},
    }
    plan = service._build_visual_plan(
        episode=episode,
        script=script,
        target_duration_seconds=60,
        visual_assets=[asset],
    )
    _mark_video_beats_people_free(plan)
    for beat in plan["beats"]:
        beat["timing_source"] = "verified_manual_excerpt"
        beat["manual_excerpt_duration_ms"] = beat["duration_ms"]
        beat["source_end_ms"] = beat["source_start_ms"] + beat["duration_ms"]
    episode.workflow_control["primer_visual_plan"] = plan
    narration = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        source_entity_type="primer_narration",
        source_entity_id="timing-track",
        status="completed",
        duration_ms=44_270,
    )

    timing = service._synchronise_visual_plan_to_narration(
        episode,
        narration,
        script,
        actor="test",
    )
    visual_state = episode.workflow_control["primer_visual_plan"]

    assert timing["review_required"] is True
    assert visual_state["status"] == "review_required"
    assert all(beat["narration_timing_review_required"] for beat in visual_state["beats"])


def test_confirming_retimed_manual_excerpts_restores_selected_clip_durations() -> None:
    service = PrimerProductionService.__new__(PrimerProductionService)
    episode = _episode_for_visual_plan()
    asset = _opening_asset(episode, AssetType.video, "Official footage")
    episode.assets.append(asset)
    script = " ".join(["Evidence-led narration."] * 120)
    episode.workflow_control["primer_production"] = {
        "script": script,
        "editorial_polish": {"status": "applied"},
    }
    plan = service._build_visual_plan(
        episode=episode,
        script=script,
        target_duration_seconds=60,
        visual_assets=[asset],
    )
    _mark_video_beats_people_free(plan)
    for index, beat in enumerate(plan["beats"]):
        start_ms = index * 10_000
        end_ms = start_ms + 10_000
        beat.update(
            {
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
                "duration_ms": 10_000,
                "timing_source": "verified_manual_excerpt",
                "manual_excerpt_duration_ms": 10_000,
            }
        )
    episode.workflow_control["primer_visual_plan"] = plan
    narration = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        source_entity_type="primer_narration",
        source_entity_id="timing-track",
        status="completed",
        duration_ms=44_270,
    )

    timing = service._synchronise_visual_plan_to_narration(
        episode,
        narration,
        script,
        actor="test",
    )

    assert timing["review_required"] is True
    assert sum(beat["duration_ms"] for beat in plan["beats"]) == 44_270

    status = asyncio.run(
        service.verify_visual_plan_excerpts(
            episode,
            PrimerVisualPlanVerificationRequest(user_id="test"),
            model_endpoints=[],
        )
    )

    assert status.coverage["ready"] is True
    assert status.coverage["planned_visual_duration_ms"] == len(plan["beats"]) * 10_000
    assert status.coverage["terminal_clip_duration_ms"] > 0
    assert all(beat["duration_ms"] == 10_000 for beat in status.beats)
    assert all(
        beat["timing_source"] == "verified_manual_excerpt" for beat in status.beats
    )
