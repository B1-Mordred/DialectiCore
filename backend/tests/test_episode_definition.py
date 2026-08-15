import pytest
from app.domain.schemas import EpisodeDefinition
from pydantic import ValidationError


def valid_definition() -> dict:
    return {
        "title": "Will AI replace software developers?",
        "topic": {
            "central_question": "How will AI change professional software development?",
            "required_dimensions": ["short_term_effects", "risks", "opportunities"],
        },
        "format": {"target_duration_minutes": 4, "participant_count": 4},
        "participants": [
            {"participant_profile_id": "host", "role": "moderator"},
            {"participant_profile_id": "optimist", "role": "panelist"},
            {"participant_profile_id": "skeptic", "role": "panelist"},
            {"participant_profile_id": "practitioner", "role": "panelist"},
        ],
    }


def test_episode_definition_accepts_increment_one_shape() -> None:
    definition = EpisodeDefinition.model_validate(valid_definition())

    assert definition.title == "Will AI replace software developers?"
    assert definition.format.participant_count == 4
    assert definition.languages.source_language == "en"
    assert definition.workflow.production_target == "native_visual"


def test_episode_definition_accepts_explicit_audio_first_target() -> None:
    data = valid_definition()
    data["workflow"] = {"production_target": "audio_first"}

    definition = EpisodeDefinition.model_validate(data)

    assert definition.workflow.production_target == "audio_first"


def test_episode_definition_rejects_unknown_production_target() -> None:
    data = valid_definition()
    data["workflow"] = {"production_target": "fallback_only"}

    with pytest.raises(ValidationError, match="production_target"):
        EpisodeDefinition.model_validate(data)


def test_episode_definition_requires_one_moderator() -> None:
    data = valid_definition()
    data["participants"][0]["role"] = "panelist"

    with pytest.raises(ValidationError, match="exactly one moderator"):
        EpisodeDefinition.model_validate(data)
