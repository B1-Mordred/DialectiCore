import pytest
from app.domain.schemas import ModelEndpoint, PrimerNarratorProfile, VoiceboxEndpoint, VoiceProfile
from app.infrastructure.repository import EpisodeRepository


def test_primer_narrator_profiles_are_reusable_and_not_cast_members() -> None:
    repo = EpisodeRepository()
    repo.upsert_model_endpoint(
        ModelEndpoint(
            id="openrouter",
            name="OpenRouter",
            provider_type="openai_compatible",
            base_url="https://models.example.test/v1",
        )
    )
    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="voicebox",
            name="Voicebox",
            adapter_type="mock",
        )
    )
    repo.upsert_voice_profile(
        VoiceProfile(
            id="voice-de",
            name="German Voice",
            voicebox_endpoint_id="voicebox",
            voice_id="de",
            language="de",
        )
    )
    profile = PrimerNarratorProfile(
        id="primer-de",
        name="German Primer Narrator",
        language="de",
        model_endpoint_id="openrouter",
        model_id="google/gemini-3.6-flash",
        voice_profile_id="voice-de",
    )

    saved = repo.upsert_primer_narrator_profile(profile)

    assert saved.id == "primer-de"
    assert repo.list_primer_narrator_profiles() == [profile]
    with pytest.raises(ValueError, match="primer narrator profiles"):
        repo.delete_voice_profile("voice-de")
    repo.delete_primer_narrator_profile("primer-de")
    assert repo.list_primer_narrator_profiles() == []
