from datetime import UTC, datetime

from app.domain.enums import ParticipantType, ProviderType
from app.domain.schemas import (
    CharacterPerformance,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    DiscussionPromptTemplate,
    LanguageProfile,
    ModelEndpoint,
    ParticipantProfile,
    PublisherTarget,
    RenderPreset,
    SamplingSettings,
    VisualProfile,
    VoiceboxEndpoint,
    VoiceProfile,
)

DEFAULT_PROMPT_VARIABLES = {
    "participant": [
        "display_name",
        "perspective",
        "expertise",
        "speaking_style",
    ],
    "turn_context": [
        "central_question",
        "phase",
        "discussion_intensity",
        "latest_host_instruction",
        "remaining_seconds",
        "required_dimensions",
        "evidence_summary",
        "available_evidence_refs",
        "tool_results",
        "public_transcript",
        "private_memory",
    ],
    "output_contract": (
        "Return only JSON matching StructuredTurnOutput: spoken_text, intent, "
        "responding_to, claims, questions_for_others, requested_follow_up, "
        "and private_memory_update."
    ),
}

OPENROUTER_ENDPOINT_ID = "openrouter"
OPENROUTER_MODEL_PRESETS = [
    "openai/gpt-4.1-mini",
    "anthropic/claude-sonnet-5",
    "google/gemini-3.6-flash",
    "x-ai/grok-4.3",
    "deepseek/deepseek-v3.2",
    "mistralai/mistral-large-2512",
]
OPENROUTER_CHARACTER_MODEL_ASSIGNMENTS = {
    "chatgpt": "openai/gpt-4.1-mini",
    "claude": "anthropic/claude-sonnet-5",
    "deepseek": "deepseek/deepseek-v3.2",
    "gemini": "google/gemini-3.6-flash",
    "grok": "x-ai/grok-4.3",
    "mistral": "mistralai/mistral-large-2512",
}


def openrouter_model_endpoint() -> ModelEndpoint:
    return ModelEndpoint(
        id=OPENROUTER_ENDPOINT_ID,
        name="OpenRouter",
        provider_type=ProviderType.openai_compatible,
        base_url="https://openrouter.ai/api/v1",
        credential_reference="env:OPENROUTER_API_KEY",
        default_timeout_seconds=300,
        max_concurrency=2,
        enabled=True,
        capabilities={
            "provider": "openrouter",
            "chat_completions": True,
            "json_schema_response": True,
            "structured_turn_output": True,
            "model_presets": OPENROUTER_MODEL_PRESETS,
            "health_path": "/models",
            "authorization_scheme": "bearer",
            "site_url": "http://userver:5173",
            "app_title": "DialectiCore",
            "minimum_structured_max_tokens": 3200,
        },
    )


def default_discussion_prompt_templates() -> list[DiscussionPromptTemplate]:
    return [
        DiscussionPromptTemplate(
            id="moderator_v2",
            version="2.0.0",
            participant_type=ParticipantType.host,
            system=(
                "You are {display_name}, the moderator. Follow this role: {perspective}. "
                "Expertise: {expertise}. Speaking style: {speaking_style}. Move the "
                "conversation forward by naming concrete claims and speakers, exposing a "
                "decision-relevant disagreement, and giving a challenged participant room to "
                "answer or revise. At high intensity, be sharp but civil: ask short direct "
                "questions, press for a criterion or counterexample, and never manufacture "
                "conflict, use insults, mock a participant, or present unsupported claims as "
                "facts. Separate evidence from judgment. Return only JSON matching the "
                "required structured turn schema. Do not include hidden reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Discussion intensity: {discussion_intensity}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this moderator:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            enabled=True,
            change_summary="Lively, evidence-bound moderator prompt for sharp but civil exchanges.",
        ),
        DiscussionPromptTemplate(
            id="panelist_v2",
            version="2.0.0",
            participant_type=ParticipantType.panelist,
            system=(
                "You are {display_name}. Follow this role: {perspective}. Expertise: "
                "{expertise}. Speaking style: {speaking_style}. Speak like a thoughtful "
                "person in a live discussion, not a policy memo: make one clear point, react "
                "to a named prior claim when relevant, and add a trade-off, counterexample, "
                "criterion, or honest revision. At high intensity, disagree directly but "
                "civilly; challenge reasoning rather than people. Do not use insults, mockery, "
                "or fabricated evidence. Cite available evidence refs for source-grounded "
                "claims and clearly label judgment or uncertainty. Return only JSON matching "
                "the required structured turn schema. Do not include hidden reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Discussion intensity: {discussion_intensity}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this panelist:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            enabled=True,
            change_summary="Lively, evidence-bound panelist prompt for sharp but civil exchanges.",
        ),
        DiscussionPromptTemplate(
            id="moderator_v1",
            version="1.0.0",
            participant_type=ParticipantType.host,
            system=(
                "You are {display_name}, the moderator. Follow this role: {perspective}. "
                "Expertise: {expertise}. Speaking style: {speaking_style}. Keep the show "
                "within the configured duration, actively connect turns to prior statements, "
                "separate evidence from judgment, and ask focused follow-up questions. "
                "Return only JSON matching the required structured turn schema. "
                "Do not include hidden reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this moderator:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            enabled=True,
            change_summary="Initial default moderator discussion prompt.",
        ),
        DiscussionPromptTemplate(
            id="panelist_v1",
            version="1.0.0",
            participant_type=ParticipantType.panelist,
            system=(
                "You are {display_name}. Follow this role: {perspective}. Expertise: "
                "{expertise}. Speaking style: {speaking_style}. Respond to the current "
                "moderator instruction and the public transcript, preserve your private "
                "memory, cite available evidence refs when making source-grounded claims, "
                "and make disagreement explicit when warranted. Return only JSON matching "
                "the required structured turn schema. Do not include hidden reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this panelist:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            enabled=True,
            change_summary="Initial default panelist discussion prompt.",
        ),
        DiscussionPromptTemplate(
            id="fact_checker_v1",
            version="1.0.0",
            participant_type=ParticipantType.fact_checker,
            system=(
                "You are {display_name}, the fact checker. Follow this role: {perspective}. "
                "Expertise: {expertise}. Speaking style: {speaking_style}. Test claims "
                "against the shared evidence pack, identify uncertainty, and return only "
                "JSON matching the required structured turn schema. Do not include hidden "
                "reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this fact checker:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            enabled=True,
            change_summary="Initial default fact-checker discussion prompt.",
        ),
        DiscussionPromptTemplate(
            id="guest_v1",
            version="1.0.0",
            participant_type=ParticipantType.guest,
            system=(
                "You are {display_name}, a guest contributor. Follow this role: "
                "{perspective}. Expertise: {expertise}. Speaking style: {speaking_style}. "
                "Bring a concrete external viewpoint, respond to the public transcript, "
                "and return only JSON matching the required structured turn schema. "
                "Do not include hidden reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this guest:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            enabled=True,
            change_summary="Initial default guest discussion prompt.",
        ),
        DiscussionPromptTemplate(
            id="audience_proxy_v1",
            version="1.0.0",
            participant_type=ParticipantType.audience_proxy,
            system=(
                "You are {display_name}, representing audience questions. Follow this "
                "role: {perspective}. Expertise: {expertise}. Speaking style: "
                "{speaking_style}. Surface clear audience concerns, ask for clarification "
                "when the panel is vague, and return only JSON matching the required "
                "structured turn schema. Do not include hidden reasoning or markdown."
            ),
            user=(
                "Central question: {central_question}\n"
                "Phase: {phase}\n"
                "Remaining seconds: {remaining_seconds}\n"
                "Latest host instruction: {latest_host_instruction}\n"
                "Required dimensions: {required_dimensions}\n"
                "Shared evidence pack:\n{evidence_summary}\n"
                "Available evidence refs: {available_evidence_refs}\n"
                "Permitted tool results:\n{tool_results}\n"
                "Public transcript:\n{public_transcript}\n"
                "Private memory for this audience proxy:\n{private_memory}"
            ),
            variables=DEFAULT_PROMPT_VARIABLES,
            created_by="dialecticore",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            enabled=True,
            change_summary="Initial default audience-proxy discussion prompt.",
        ),
    ]


def default_language_profiles() -> list[LanguageProfile]:
    return [
        LanguageProfile(
            id="en",
            name="English",
            bcp47_tag="en",
            native_name="English",
            default_mode="canonical",
            line_breaking={"max_chars_per_line": 42, "max_lines": 2},
            voice_defaults={"speaking_rate": 1.0},
        ),
        LanguageProfile(
            id="de",
            name="German",
            bcp47_tag="de",
            native_name="Deutsch",
            default_mode="localized_reperformance",
            line_breaking={"max_chars_per_line": 38, "max_lines": 2},
            voice_defaults={"speaking_rate": 0.95},
        ),
    ]


def default_model_endpoints() -> list[ModelEndpoint]:
    return [
        ModelEndpoint(
            id="mock",
            name="Deterministic Mock Provider",
            provider_type=ProviderType.mock,
            base_url=None,
            health_status="healthy",
            capabilities={"structured_output": True, "tool_use": False},
        )
    ]


def default_voicebox_endpoints() -> list[VoiceboxEndpoint]:
    return [
        VoiceboxEndpoint(
            id="mock-voicebox",
            name="Deterministic Mock Voicebox",
            adapter_type="mock",
            health_status="healthy",
            capabilities={
                "tts": True,
                "word_timestamps": True,
                "phoneme_timestamps": False,
                "formats": ["audio/wav"],
            },
        ),
    ]


B1_GERMAN_VOICE_PRESETS = (
    (
        "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
        "A_DE_Claude",
        "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
    ),
    (
        "67a00466-17ba-4f26-812e-60c13119be9e",
        "A_DE_DeepSeek",
        "85ff8fac-a53c-4385-a1d5-5930d3a142aa",
    ),
    (
        "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
        "A_DE_Grok",
        "3eee56a8-119b-4ed8-b829-72efe51a8be6",
    ),
    (
        "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
        "A_DE_Gemini",
        "6e70eabc-ee1c-4a5d-a488-4094d4384507",
    ),
    (
        "7476947f-5836-480b-9a95-67bf66575c2a",
        "A_DE_Mistral",
        "4e2a637d-575d-446e-b3d6-7141d655a4e6",
    ),
    (
        "1865b646-41ca-4140-ba9d-1a40d9fe623a",
        "A_ChatGPT",
        "1865b646-41ca-4140-ba9d-1a40d9fe623a",
    ),
)
B1_CHARACTER_VOICE_ASSIGNMENTS = {
    "chatgpt": "1865b646-41ca-4140-ba9d-1a40d9fe623a",
    "claude": "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
    "deepseek": "67a00466-17ba-4f26-812e-60c13119be9e",
    "gemini": "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
    "grok": "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
    "mistral": "7476947f-5836-480b-9a95-67bf66575c2a",
}


def b1_german_voice_profiles(
    voicebox_endpoint_id: str = "b1-voicebox",
) -> list[VoiceProfile]:
    return [
        VoiceProfile(
            id=profile_id,
            name=name,
            voicebox_endpoint_id=voicebox_endpoint_id,
            voice_id=remote_profile_id,
            language="de",
            speaker_label=name,
            model_id="chatterbox",
            prosody={"engine": "chatterbox", "normalize": False, "effects_chain": []},
            rate=1,
            pitch=0,
            pronunciation_dictionary={},
            enabled=True,
        )
        for profile_id, name, remote_profile_id in B1_GERMAN_VOICE_PRESETS
    ]


def default_voice_profiles() -> list[VoiceProfile]:
    return [
        VoiceProfile(
            id="voice-chatgpt",
            name="ChatGPT Mock Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-chatgpt",
            language="en",
            speaker_label="ChatGPT",
            prosody={"style": "warm_moderator"},
        ),
        VoiceProfile(
            id="voice-claude",
            name="Claude Mock Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-claude",
            language="en",
            speaker_label="Claude",
            prosody={"style": "measured_reflective"},
        ),
        VoiceProfile(
            id="voice-deepseek",
            name="DeepSeek Mock Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-deepseek",
            language="en",
            speaker_label="DeepSeek",
            prosody={"style": "technical_precise"},
        ),
        VoiceProfile(
            id="voice-grok",
            name="Grok Mock Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-grok",
            language="en",
            speaker_label="Grok",
            prosody={"style": "brisk_contrarian"},
        ),
        VoiceProfile(
            id="voice-gemini",
            name="Gemini Mock Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-gemini",
            language="en",
            speaker_label="Gemini",
            prosody={"style": "polished_connective"},
        ),
        VoiceProfile(
            id="voice-mistral",
            name="Mistral Mock Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-mistral",
            language="en",
            speaker_label="Mistral",
            prosody={"style": "concise_practical"},
        ),
        VoiceProfile(
            id="voice-host",
            name="Moderator Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-moderator",
            language="en",
            speaker_label="Moderator",
            prosody={"style": "measured"},
        ),
        VoiceProfile(
            id="voice-optimist",
            name="Optimist Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-optimist",
            language="en",
            speaker_label="The Optimist",
            prosody={"style": "bright"},
        ),
        VoiceProfile(
            id="voice-skeptic",
            name="Skeptic Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-skeptic",
            language="en",
            speaker_label="The Skeptic",
            prosody={"style": "controlled"},
        ),
        VoiceProfile(
            id="voice-practitioner",
            name="Practitioner Voice",
            voicebox_endpoint_id="mock-voicebox",
            voice_id="mock-practitioner",
            language="en",
            speaker_label="The Practitioner",
            prosody={"style": "grounded"},
        ),
    ]


def default_comfyui_endpoints() -> list[ComfyUiEndpoint]:
    return [
        ComfyUiEndpoint(
            id="mock-comfyui",
            name="Deterministic Mock ComfyUI",
            adapter_type="mock",
            health_status="healthy",
            capabilities={
                "prompt": True,
                "history": True,
                "queue": True,
                "image": True,
                "video": True,
            },
        )
    ]


def default_comfyui_workflows() -> list[ComfyUiWorkflow]:
    return [
        ComfyUiWorkflow(
            id="workflow-talking-head-v1",
            name="Talking Head Video v1",
            workflow_type="talking_head",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="video",
            api_workflow=_comfyui_api_workflow_template("talking_head"),
            prompt_template={
                "positive": (
                    "{character_name}, {style_prompt}, speaking in a studio, "
                    "frontal medium close-up, natural mouth articulation, "
                    "consistent identity, broadcast lighting"
                ),
                "negative": (
                    "{negative_prompt}, distorted face, unreadable text, "
                    "lip-sync drift, identity shift, duplicate person"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("talking_head"),
            },
            default_parameters={
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "duration_ms": 4500,
                "steps": 28,
                "cfg": 7.5,
                "filename_prefix": "dialecticore/talking-head",
                "motion_bucket_id": 96,
                "camera_motion": "locked_medium_closeup",
                "lighting_preset": "soft_key_fill_rim",
                "workflow_preset": "talking_head_video_v1",
                "b1_media_preset": "talking-head-lipsync",
                "b1_media_operation": "talking-head-lipsync",
                "b1_media_family": "MuseTalk audio-driven talking-head lipsync",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "b1_lipsync_width": 512,
                "b1_lipsync_height": 512,
                "b1_lipsync_fps": 12,
                "workflow_capabilities": {
                    "reference_image_conditioning": True,
                    "mouth_motion_guidance": True,
                    "audio_driven_lipsync": True,
                    "temporal_consistency": True,
                    "video_output": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-seated-panel-lipsync-v1",
            name="Seated Panel Lip-sync v1",
            workflow_type="seated_panel_lipsync",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="video",
            api_workflow=_comfyui_api_workflow_template("talking_head"),
            prompt_template={
                "positive": (
                    "{character_name} speaking naturally from their assigned seat in the "
                    "configured studio panel, consistent identity, broadcast camera coverage"
                ),
                "negative": (
                    "{negative_prompt}, portrait card, black background, duplicate person, "
                    "identity shift, lip-sync drift, floating overlay"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("talking_head"),
            },
            default_parameters={
                "width": 1024,
                "height": 576,
                "fps": 12,
                "duration_ms": 4500,
                "workflow_preset": "seated_panel_lipsync_v1",
                "b1_media_preset": "talking-head-lipsync",
                "b1_media_operation": "talking-head-lipsync",
                "b1_media_family": "MuseTalk audio-driven seated panel lipsync",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "b1_lipsync_width": 1024,
                "b1_lipsync_height": 576,
                "b1_lipsync_fps": 12,
                "workflow_capabilities": {
                    "audio_driven_lipsync": True,
                    "identity_preserving_scene_conditioning": True,
                    "seated_panel_scene": True,
                    "native_scene_camera": True,
                    "camera_views": [
                        "establishing_wide",
                        "speaker_medium",
                        "speaker_close",
                        "panel_two_shot",
                        "reaction",
                    ],
                    "wall_screen_compositing": True,
                    "video_output": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-studio-seated-character-p40-v2",
            name="Studio Seated Character P40 v2",
            workflow_type="studio_seated_character",
            comfyui_endpoint_id="b1-comfyui",
            output_asset_type="image",
            api_workflow=_comfyui_api_workflow_template("topic_broll"),
            prompt_template={
                "positive": (
                    "{character_name} seated naturally in the assigned studio chair, "
                    "identity preserved, neutral seated pose, front three-quarter camera, "
                    "transparent matte plate"
                ),
                "negative": (
                    "standing, floating, chair contamination, duplicate person, extra limbs, "
                    "identity shift, opaque card, text, logo"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("topic_broll"),
            },
            default_parameters={
                "width": 1280,
                "height": 720,
                "workflow_preset": "studio_seated_character_p40_v2",
                "b1_media_preset": "studio-seated-character-p40",
                "b1_media_operation": "studio-seated-character",
                "b1_media_family": "P40 identity-preserving native seated character plate",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "b1_media_runtime_policy": "any",
                "b1_media_width": 1280,
                "b1_media_height": 720,
                "workflow_capabilities": {
                    "identity_preserving": True,
                    "openpose_seated_conditioning": True,
                    "alpha_matted_output": True,
                    "human_review_required": True,
                    "scheduler_managed_p40": True,
                    "native_1280x720": True,
                    "image_output": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-studio-panel-shot-v1",
            name="Studio Panel Keyframe v1",
            workflow_type="studio_panel_shot",
            comfyui_endpoint_id="b1-comfyui",
            output_asset_type="studio_scene",
            api_workflow=_comfyui_api_workflow_template("studio_wide_shot"),
            prompt_template={
                "positive": (
                    "identity-preserving seated discussion panel in the configured studio, "
                    "{camera_view} camera, visible table, empty rear display area, "
                    "broadcast lighting, all assigned participants in fixed seats"
                ),
                "negative": (
                    "portrait cards, black background, standing figures, duplicate people, "
                    "extra limbs, unreadable screen text, logos"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("studio_wide_shot"),
            },
            default_parameters={
                "width": 1280,
                "height": 720,
                "workflow_preset": "studio_panel_keyframe_v1",
                "b1_media_preset": "studio-panel-shot",
                "b1_media_operation": "studio-panel-shot",
                "b1_media_family": "Identity-preserving seated studio panel keyframe",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "b1_media_width": 1280,
                "b1_media_height": 720,
                "workflow_capabilities": {
                    "multi_character_identity_conditioning": True,
                    "fixed_seating": True,
                    "face_region_metadata": True,
                    "wall_screen_geometry": True,
                    "image_output": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-reaction-v1",
            name="Reaction Shot v1",
            workflow_type="reaction_shot",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="video",
            api_workflow=_comfyui_api_workflow_template("reaction_shot"),
            prompt_template={
                "positive": (
                    "{character_name}, {style_prompt}, attentive listening reaction, "
                    "subtle expression change, over-the-shoulder studio cutaway, "
                    "consistent identity"
                ),
                "negative": (
                    "{negative_prompt}, distorted face, unreadable text, "
                    "talking mouth, exaggerated expression, identity shift"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("reaction_shot"),
            },
            default_parameters={
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "duration_ms": 3000,
                "steps": 22,
                "cfg": 6.5,
                "filename_prefix": "dialecticore/reaction",
                "motion_bucket_id": 72,
                "camera_motion": "subtle_reaction_push",
                "lighting_preset": "soft_panel_cutaway",
                "workflow_preset": "reaction_loop_v1",
                "b1_media_preset": "video-image",
                "b1_media_family": "Wan 2.1 VACE 1.3B image/video-conditioned workflows",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "b1_media_width": 256,
                "b1_media_height": 256,
                "b1_media_fps": 12,
                # B1 Wan VACE currently accepts 5-33 frames. At 12 fps this
                # yields a safe 30-frame reaction loop.
                "b1_media_duration_ms": 2500,
                "b1_reference_artifact_fields": {
                    "default": "source_image_artifact_id",
                },
                "workflow_capabilities": {
                    "reference_image_conditioning": True,
                    "loopable_motion": True,
                    "temporal_consistency": True,
                    "video_output": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-topic-broll-v1",
            name="Topic B-roll v1",
            workflow_type="topic_broll",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="broll",
            api_workflow=_comfyui_api_workflow_template("topic_broll"),
            prompt_template={
                "positive": (
                    "editorial B-roll for {topic}, documentary insert shot, "
                    "abstract supporting visuals, clean composition, no generated text"
                ),
                "negative": (
                    "logos, unreadable text, misleading chart labels, fake UI, "
                    "sensational imagery"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("topic_broll"),
            },
            default_parameters={
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "steps": 30,
                "cfg": 7.0,
                "shot_style": "editorial_insert",
                "composition": "safe_center_weighted_for_picture_in_picture",
                "lighting_preset": "documentary_neutral",
                "workflow_preset": "topic_broll_image_v1",
                "b1_media_preset": "image-default",
                "b1_media_family": "SD 1.5 text-to-image",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                # Rear-screen stills are rendered only within the studio wall
                # display. Keep the B1 SD 1.5 job within the appliance limit.
                "b1_media_width": 512,
                "b1_media_height": 288,
                "workflow_capabilities": {
                    "source_grounded_prompting": True,
                    "image_output": True,
                    "text_avoidance": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-studio-wide-v1",
            name="Studio Wide Shot v1",
            workflow_type="studio_wide_shot",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="studio_scene",
            api_workflow=_comfyui_api_workflow_template("studio_wide_shot"),
            prompt_template={
                "positive": (
                    "wide shot of an analytical panel studio, controlled lighting, "
                    "four-seat discussion set, clean background, broadcast composition"
                ),
                "negative": (
                    "unreadable text, extra limbs, distorted faces, cluttered set, "
                    "brand logos"
                ),
                "node_input_bindings": _comfyui_node_input_bindings("studio_wide_shot"),
            },
            default_parameters={
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "duration_ms": 4000,
                "steps": 26,
                "cfg": 7.0,
                "filename_prefix": "dialecticore/studio",
                "motion_bucket_id": 48,
                "camera_motion": "slow_establishing_dolly",
                "lighting_preset": "balanced_studio_grid",
                "workflow_preset": "studio_wide_scene_v1",
                "b1_media_preset": "video-image",
                "b1_media_family": "Wan 2.1 VACE 1.3B image/video-conditioned workflows",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "b1_media_width": 256,
                "b1_media_height": 256,
                "b1_media_fps": 12,
                # B1 Wan VACE currently accepts 5-33 frames. At 12 fps this
                # yields a safe 30-frame studio loop.
                "b1_media_duration_ms": 2500,
                "b1_reference_artifact_fields": {
                    "default": "source_image_artifact_id",
                },
                "workflow_capabilities": {
                    "set_consistency": True,
                    "loopable_motion": True,
                    "video_output": True,
                },
            },
        ),
        ComfyUiWorkflow(
            id="workflow-image-edit-v1",
            name="Image Edit/Inpaint v1",
            workflow_type="image_edit",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="image",
            api_workflow={},
            prompt_template={
                "positive": "{style_prompt}, preserve character identity, targeted edit",
                "negative": "{negative_prompt}, identity drift, artifacts, unreadable text",
            },
            default_parameters={
                "workflow_preset": "image_edit_v1",
                "b1_media_preset": "image-edit",
                "b1_media_family": "SD 1.5 image edit/inpaint workflows",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "workflow_capabilities": {
                    "reference_image_conditioning": True,
                    "mask_conditioning": True,
                    "image_output": True,
                    "managed_b1_media_api": True,
                },
            },
            enabled=False,
        ),
        ComfyUiWorkflow(
            id="workflow-image-upscale-v1",
            name="Image Upscale x4plus v1",
            workflow_type="image_upscale",
            comfyui_endpoint_id="mock-comfyui",
            output_asset_type="image",
            api_workflow={},
            prompt_template={},
            default_parameters={
                "workflow_preset": "image_upscale_x4plus_v1",
                "b1_media_preset": "image-upscale",
                "b1_media_family": "Real-ESRGAN x4plus",
                "b1_managed_api_base": "https://api.ai.b1.germering",
                "managed_b1_media_api": True,
                "workflow_capabilities": {
                    "upscale_factor": 4,
                    "image_output": True,
                    "managed_b1_media_api": True,
                },
            },
            enabled=False,
        ),
    ]


def default_render_presets() -> list[RenderPreset]:
    return [
        RenderPreset(
            id="youtube-1080p",
            name="YouTube 1080p",
            width=1920,
            height=1080,
            fps=30,
            video_bitrate="8M",
            audio_bitrate="192k",
        ),
        RenderPreset(
            id="youtube-1440p",
            name="YouTube 1440p",
            width=2560,
            height=1440,
            fps=30,
            video_bitrate="16M",
            audio_bitrate="192k",
        ),
        RenderPreset(
            id="youtube-4k",
            name="YouTube 4K",
            width=3840,
            height=2160,
            fps=30,
            video_bitrate="35M",
            audio_bitrate="256k",
        ),
        RenderPreset(
            id="preview-low-bitrate",
            name="Preview Low Bitrate",
            width=1280,
            height=720,
            fps=24,
            video_bitrate="2M",
            audio_bitrate="128k",
        ),
        RenderPreset(
            id="audio-only",
            name="Audio Only",
            width=1280,
            height=720,
            fps=1,
            video_bitrate="256k",
            audio_bitrate="192k",
            render_scope="audio",
        ),
        RenderPreset(
            id="short-promotional-clip",
            name="Short Promotional Clip",
            width=1080,
            height=1920,
            fps=30,
            video_bitrate="6M",
            audio_bitrate="160k",
        ),
    ]


def default_publisher_targets() -> list[PublisherTarget]:
    return [
        PublisherTarget(
            id="mock-youtube",
            name="Mock YouTube Publisher",
            platform="youtube",
            adapter_type="mock",
            channel_id="dialecticore-demo-channel",
            privacy_status="unlisted",
            default_language="en",
            default_tags=["DialectiCore"],
            health_status="healthy",
            capabilities={
                "dry_run": True,
                "metadata_upload": True,
                "video_upload": False,
                "thumbnail_upload": False,
                "subtitle_upload": False,
            },
        ),
        PublisherTarget(
            id="youtube-resumable",
            name="YouTube Resumable Upload",
            platform="youtube",
            adapter_type="youtube_resumable",
            base_url="https://www.googleapis.com",
            credential_reference="env:YOUTUBE_OAUTH_ACCESS_TOKEN",
            channel_id="youtube-channel-reference",
            privacy_status="unlisted",
            default_language="en",
            default_tags=["DialectiCore"],
            enabled=False,
            health_status="unconfigured",
            capabilities={
                "dry_run": True,
                "metadata_upload": True,
                "video_upload": True,
                "resumable_upload": True,
                "thumbnail_upload": True,
                "subtitle_upload": True,
                "caption_upload": True,
                "oauth_required": True,
                "upload_path": (
                    "/upload/youtube/v3/videos?"
                    "uploadType=resumable&part=snippet,status"
                ),
                "thumbnail_upload_path": "/upload/youtube/v3/thumbnails/set",
                "caption_upload_path": "/upload/youtube/v3/captions?part=snippet",
                "oauth_refresh_token_reference": "env:YOUTUBE_OAUTH_REFRESH_TOKEN",
                "oauth_client_id_reference": "env:YOUTUBE_OAUTH_CLIENT_ID",
                "oauth_client_secret_reference": "env:YOUTUBE_OAUTH_CLIENT_SECRET",
                "oauth_token_url": "https://oauth2.googleapis.com/token",
                "health_path": "/youtube/v3/channels?part=id&mine=true",
            },
        ),
    ]


def _comfyui_node_input_bindings(workflow_type: str) -> dict[str, str]:
    bindings = {
        "6.inputs.text": "positive_prompt",
        "7.inputs.text": "negative_prompt",
        "8.inputs.width": "width",
        "8.inputs.height": "height",
        "9.inputs.seed": "seed",
        "9.inputs.steps": "steps",
        "9.inputs.cfg": "cfg",
        "13.inputs.text": "style_prompt",
        "14.inputs.text": "transcript_text",
    }
    if workflow_type in {"talking_head", "reaction_shot", "studio_wide_shot"}:
        bindings["8.inputs.batch_size"] = "frame_count"
        bindings["10.inputs.fps"] = "fps"
        bindings["12.inputs.filename_prefix"] = "filename_prefix"
        bindings["15.inputs.camera_motion"] = "camera_motion"
        bindings["16.inputs.lighting_preset"] = "lighting_preset"
    if workflow_type == "topic_broll":
        bindings["15.inputs.shot_style"] = "shot_style"
        bindings["16.inputs.composition"] = "composition"
        bindings["17.inputs.lighting_preset"] = "lighting_preset"
    return bindings


def _comfyui_api_workflow_template(workflow_type: str) -> dict:
    base = {
        "3": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "sd-v1-5-pruned-emaonly-b1-video.safetensors"},
        },
        "5": {
            "class_type": "CLIPSetLastLayer",
            "inputs": {"clip": ["3", 1], "stop_at_clip_layer": -2},
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["5", 0], "text": ""}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["5", 0], "text": ""}},
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 0,
                "steps": 24,
                "cfg": 7,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
                "model": ["3", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["8", 0],
            },
        },
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 2]}},
        "13": {"class_type": "PrimitiveString", "inputs": {"text": ""}},
        "14": {"class_type": "PrimitiveString", "inputs": {"text": ""}},
        "18": {
            "class_type": "ConditioningConcat",
            "inputs": {"conditioning_to": ["6", 0], "conditioning_from": ["13", 0]},
        },
    }
    if workflow_type == "topic_broll":
        return {
            **base,
            "8": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 0, "height": 0, "batch_size": 1},
            },
            "12": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "dialecticore/broll", "images": ["11", 0]},
            },
            "15": {"class_type": "PrimitiveString", "inputs": {"shot_style": ""}},
            "16": {"class_type": "PrimitiveString", "inputs": {"composition": ""}},
            "17": {"class_type": "PrimitiveString", "inputs": {"lighting_preset": ""}},
            "19": {
                "class_type": "ImageMetadata",
                "inputs": {
                    "images": ["11", 0],
                    "topic": ["14", 0],
                    "shot_style": ["15", 0],
                    "composition": ["16", 0],
                    "lighting_preset": ["17", 0],
                },
            },
        }
    if workflow_type == "studio_wide_shot":
        return {
            **base,
            "8": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 0, "height": 0, "batch_size": 1},
            },
            "10": {
                "class_type": "CreateVideo",
                "inputs": {"images": ["11", 0], "fps": 0},
            },
            "12": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": ["10", 0],
                    "filename_prefix": "dialecticore/studio",
                    "format": "mp4",
                    "codec": "h264",
                },
            },
            "15": {"class_type": "PrimitiveString", "inputs": {"camera_motion": ""}},
            "16": {"class_type": "PrimitiveString", "inputs": {"lighting_preset": ""}},
        }
    length = 135 if workflow_type == "talking_head" else 90
    prefix = "dialecticore/talking-head" if workflow_type == "talking_head" else (
        "dialecticore/reaction"
    )
    return {
        **base,
        "8": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 0, "height": 0, "batch_size": length},
        },
        "10": {
            "class_type": "CreateVideo",
            "inputs": {"images": ["11", 0], "fps": 0},
        },
        "12": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["10", 0],
                "filename_prefix": prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
        "15": {"class_type": "PrimitiveString", "inputs": {"camera_motion": ""}},
        "16": {"class_type": "PrimitiveString", "inputs": {"lighting_preset": ""}},
    }


def default_visual_profiles() -> list[VisualProfile]:
    return [
        VisualProfile(
            id="visual-chatgpt",
            name="ChatGPT Visual",
            character_name="ChatGPT",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt=(
                "warm analytical AI moderator, composed expression, clear eye contact, "
                "modern broadcast studio, pragmatic and helpful presence"
            ),
            negative_prompt="salesy mascot, generic robot, distorted face, exaggerated grin",
            seed=1101,
            performance=CharacterPerformance(
                on_camera_energy="measured", gaze_style="steady", head_motion="subtle",
                expression_range="warm", gesture_frequency="occasional",
                signature_habit="small acknowledging nod before a synthesis", variation_seed=2101,
            ),
        ),
        VisualProfile(
            id="visual-claude",
            name="Claude Visual",
            character_name="Claude",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt=(
                "thoughtful constitutional AI panelist, careful posture, reflective "
                "expression, calm studio lighting"
            ),
            negative_prompt="stiff corporate avatar, smug expression, distorted face",
            seed=1102,
            performance=CharacterPerformance(
                on_camera_energy="restrained", gaze_style="reflective", head_motion="minimal",
                expression_range="contained", gesture_frequency="none",
                signature_habit="brief thoughtful pause before a careful qualification",
                variation_seed=2102,
            ),
        ),
        VisualProfile(
            id="visual-deepseek",
            name="DeepSeek Visual",
            character_name="DeepSeek",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt=(
                "efficient technical reasoning panelist, precise expression, focused "
                "engineering presence, modern studio"
            ),
            negative_prompt="hacker cliche, dark hooded figure, distorted face",
            seed=1103,
            performance=CharacterPerformance(
                on_camera_energy="measured", gaze_style="steady", head_motion="minimal",
                expression_range="contained", gesture_frequency="occasional",
                signature_habit="one precise confirming nod when citing a condition",
                variation_seed=2103,
            ),
        ),
        VisualProfile(
            id="visual-grok",
            name="Grok Visual",
            character_name="Grok",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt=(
                "irreverent fast-talking AI panelist, alert expression, skeptical humor, "
                "broadcast studio realism"
            ),
            negative_prompt="cartoon rebel, chaotic set, distorted face, meme text",
            seed=1104,
            performance=CharacterPerformance(
                on_camera_energy="engaged", gaze_style="responsive", head_motion="expressive",
                expression_range="animated", gesture_frequency="frequent",
                signature_habit="quick skeptical head tilt at a contested premise",
                variation_seed=2104,
            ),
        ),
        VisualProfile(
            id="visual-gemini",
            name="Gemini Visual",
            character_name="Gemini",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt=(
                "multimodal systems thinker panelist, polished expression, expansive "
                "context-aware presence, bright studio"
            ),
            negative_prompt="abstract twin gimmick, brand logo, distorted face",
            seed=1105,
            performance=CharacterPerformance(
                on_camera_energy="measured", gaze_style="reflective", head_motion="subtle",
                expression_range="warm", gesture_frequency="occasional",
                signature_habit="opens slightly before connecting two viewpoints",
                variation_seed=2105,
            ),
        ),
        VisualProfile(
            id="visual-mistral",
            name="Mistral Visual",
            character_name="Mistral",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt=(
                "concise European open-model panelist, direct expression, elegant "
                "technical confidence, modern studio"
            ),
            negative_prompt="fashion editorial pose, brand logo, distorted face",
            seed=1106,
            performance=CharacterPerformance(
                on_camera_energy="engaged", gaze_style="steady", head_motion="subtle",
                expression_range="contained", gesture_frequency="occasional",
                signature_habit="brief decisive nod at the end of a concise point",
                variation_seed=2106,
            ),
        ),
        VisualProfile(
            id="visual-host",
            name="Moderator Visual",
            character_name="Moderator",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt="calm professional moderator in a modern studio",
            negative_prompt="caricature, exaggerated expression",
            seed=1001,
        ),
        VisualProfile(
            id="visual-optimist",
            name="Optimist Visual",
            character_name="The Optimist",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt="engaged panelist with open expression in a modern studio",
            negative_prompt="cartoonish, exaggerated smile",
            seed=1002,
        ),
        VisualProfile(
            id="visual-skeptic",
            name="Skeptic Visual",
            character_name="The Skeptic",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt="focused panelist with precise expression in a modern studio",
            negative_prompt="aggressive pose, caricature",
            seed=1003,
        ),
        VisualProfile(
            id="visual-practitioner",
            name="Practitioner Visual",
            character_name="The Practitioner",
            primary_workflow_id="workflow-talking-head-v1",
            reaction_workflow_id="workflow-reaction-v1",
            broll_workflow_id="workflow-topic-broll-v1",
            style_prompt="grounded practitioner panelist in a modern studio",
            negative_prompt="casual selfie, distorted hands",
            seed=1004,
        ),
    ]


def default_participants() -> list[ParticipantProfile]:
    return [
        ParticipantProfile(
            id="chatgpt",
            name="chatgpt",
            display_name="ChatGPT",
            participant_type=ParticipantType.host,
            model_endpoint_id="mock",
            model_id="mock-chatgpt-v1",
            system_prompt_template="moderator_v2",
            perspective=(
                "helpful generalist moderator who synthesizes competing views and keeps "
                "the conversation concrete"
            ),
            expertise="broad reasoning, synthesis, developer workflows, product tradeoffs",
            speaking_style="structured, accessible, pragmatic, gently probing",
            sampling_settings=SamplingSettings(temperature=0.45, max_tokens=420),
            visual_profile_id="visual-chatgpt",
        ),
        ParticipantProfile(
            id="claude",
            name="claude",
            display_name="Claude",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-claude-v1",
            system_prompt_template="panelist_v2",
            perspective=(
                "careful safety-conscious analyst who emphasizes nuance, constraints, "
                "and human consequences"
            ),
            expertise="policy reasoning, risk analysis, long-form synthesis, evaluation",
            speaking_style="measured, reflective, precise, caveat-aware",
            sampling_settings=SamplingSettings(temperature=0.42, max_tokens=380),
            visual_profile_id="visual-claude",
        ),
        ParticipantProfile(
            id="deepseek",
            name="deepseek",
            display_name="DeepSeek",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-deepseek-v1",
            system_prompt_template="panelist_v2",
            perspective=(
                "cost-efficient technical reasoner who pushes for implementation detail, "
                "benchmarks, and operational feasibility"
            ),
            expertise="engineering tradeoffs, optimization, code reasoning, model efficiency",
            speaking_style="direct, technical, economical, evidence-seeking",
            sampling_settings=SamplingSettings(temperature=0.38, max_tokens=360),
            visual_profile_id="visual-deepseek",
        ),
        ParticipantProfile(
            id="grok",
            name="grok",
            display_name="Grok",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-grok-v1",
            system_prompt_template="panelist_v2",
            perspective=(
                "irreverent contrarian who challenges sanitized consensus and asks what "
                "is being ignored"
            ),
            expertise="rapid critique, current-culture framing, adversarial questioning",
            speaking_style="brisk, witty, skeptical, blunt but useful",
            sampling_settings=SamplingSettings(temperature=0.62, max_tokens=360),
            visual_profile_id="visual-grok",
        ),
        ParticipantProfile(
            id="gemini",
            name="gemini",
            display_name="Gemini",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-gemini-v1",
            system_prompt_template="panelist_v2",
            perspective=(
                "multimodal systems thinker who connects product, data, media, and user "
                "experience implications"
            ),
            expertise="multimodal reasoning, search context, product ecosystems, UX",
            speaking_style="broad, connective, polished, scenario-oriented",
            sampling_settings=SamplingSettings(temperature=0.5, max_tokens=380),
            visual_profile_id="visual-gemini",
        ),
        ParticipantProfile(
            id="mistral",
            name="mistral",
            display_name="Mistral",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-mistral-v1",
            system_prompt_template="panelist_v2",
            perspective=(
                "open-model pragmatist who values concise answers, sovereignty, and "
                "deployable systems"
            ),
            expertise="open models, enterprise deployment, latency, data control",
            speaking_style="concise, sharp, practical, lightly formal",
            sampling_settings=SamplingSettings(temperature=0.46, max_tokens=340),
            visual_profile_id="visual-mistral",
        ),
        ParticipantProfile(
            id="host",
            name="host",
            display_name="Moderator",
            participant_type=ParticipantType.host,
            model_endpoint_id="mock",
            model_id="mock-host-v1",
            system_prompt_template="moderator_v1",
            perspective="balanced moderator",
            expertise="discussion facilitation and synthesis",
            speaking_style="direct, concise, evidence-aware",
            sampling_settings=SamplingSettings(temperature=0.4, max_tokens=350),
            voice_profile_id="voice-host",
            visual_profile_id="visual-host",
        ),
        ParticipantProfile(
            id="optimist",
            name="optimist",
            display_name="The Optimist",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-optimist-v1",
            system_prompt_template="panelist_v1",
            perspective="technology adoption can improve work when governed well",
            expertise="productivity, tooling, organizational change",
            speaking_style="constructive and specific",
            voice_profile_id="voice-optimist",
            visual_profile_id="visual-optimist",
        ),
        ParticipantProfile(
            id="skeptic",
            name="skeptic",
            display_name="The Skeptic",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-skeptic-v1",
            system_prompt_template="panelist_v1",
            perspective="automation claims should be tested against costs and failure modes",
            expertise="risk, labor economics, quality control",
            speaking_style="precise and challenging",
            voice_profile_id="voice-skeptic",
            visual_profile_id="visual-skeptic",
        ),
        ParticipantProfile(
            id="practitioner",
            name="practitioner",
            display_name="The Practitioner",
            participant_type=ParticipantType.panelist,
            model_endpoint_id="mock",
            model_id="mock-practitioner-v1",
            system_prompt_template="panelist_v1",
            perspective="day-to-day engineering practice matters more than broad slogans",
            expertise="software delivery, code review, team process",
            speaking_style="grounded and pragmatic",
            voice_profile_id="voice-practitioner",
            visual_profile_id="visual-practitioner",
        ),
    ]
