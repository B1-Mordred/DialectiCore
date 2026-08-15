from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from app.core.credentials import normalize_credential_reference
from app.core.redaction import safe_provider_response_payload
from app.domain.enums import (
    AssetType,
    EpisodeStatus,
    ParticipantType,
    ProviderType,
    QualitySeverity,
    TranscriptType,
    TurnType,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BaseUrlWithoutUserInfo(BaseModel):
    @field_validator("base_url", mode="after", check_fields=False)
    @classmethod
    def base_url_must_not_include_userinfo(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value.strip())
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "base_url must not include username or password; use credential_reference"
            )
        return value

    @field_validator("credential_reference", mode="after", check_fields=False)
    @classmethod
    def credential_reference_must_be_scheme_reference(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return value
        return normalize_credential_reference(value)

    @field_validator("capabilities", mode="after", check_fields=False)
    @classmethod
    def capabilities_must_not_store_secret_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        safe_value = safe_provider_response_payload(value)
        return safe_value if isinstance(safe_value, dict) else {}


class TopicDefinition(BaseModel):
    central_question: str = Field(min_length=8)
    scope: list[str] = Field(default_factory=list)
    required_dimensions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)


class FormatDefinition(BaseModel):
    show_format_id: str = "analytical_panel_v1"
    target_duration_minutes: int = Field(default=4, ge=1, le=240)
    permitted_deviation_percent: int = Field(default=10, ge=0, le=50)
    participant_count: int = Field(default=4, ge=2, le=12)
    host_control: str = "medium"
    allow_interruptions: bool = False
    allow_follow_up_questions: bool = True
    maximum_monologue_seconds: int = Field(default=75, ge=10, le=600)
    discussion_intensity: Literal["low", "medium", "high"] = "high"

    @field_validator("discussion_intensity", mode="before")
    @classmethod
    def normalize_discussion_intensity(cls, value: object) -> str:
        """Keep historic persisted definitions readable while constraining new work."""
        normalized = str(value or "medium").strip().lower()
        return normalized if normalized in {"low", "medium", "high"} else "medium"


class DurationAllocation(BaseModel):
    introduction: float = 0.07
    opening_positions: float = 0.16
    main_discussion: float = 0.50
    challenges: float = 0.17
    closing_statements: float = 0.07
    host_summary: float = 0.03

    @model_validator(mode="after")
    def percentages_sum_to_one(self) -> DurationAllocation:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"duration allocation must sum to 1.0, got {total:.3f}")
        return self


class LanguageOutput(BaseModel):
    language: str = Field(min_length=2, max_length=12)
    mode: str = "canonical"


class LanguageDefinition(BaseModel):
    source_language: str = Field(default="en", min_length=2, max_length=12)
    outputs: list[LanguageOutput] = Field(default_factory=lambda: [LanguageOutput(language="en")])
    semantic_fidelity_threshold: float = Field(default=0.92, ge=0, le=1)
    allow_new_claims: bool = False


class ParticipantAssignment(BaseModel):
    participant_profile_id: str = Field(min_length=1)
    role: str = Field(min_length=1)


class ResearchDefinition(BaseModel):
    enabled: bool = False
    depth: str = "standard"
    require_source_links: bool = False
    approval_required: bool = False


class PrimerMediaDiscoveryDefinition(BaseModel):
    """Episode-scoped AI and acquisition policy for the topic-primer media."""

    enabled: bool = True
    model_endpoint_id: str | None = None
    model_id: str | None = None
    max_candidates: int = Field(default=6, ge=1, le=12)
    acquisition_policy: Literal["review_before_download", "automatic_official_only"] = (
        "review_before_download"
    )


class PrimerVisualPlannerDefinition(BaseModel):
    """Episode-scoped policy for AI-assisted primer storyboarding."""

    enabled: bool = True
    model_endpoint_id: str | None = None
    model_id: str | None = None
    automatic_draft_render: bool = True
    target_shot_duration_seconds: int = Field(default=6, ge=2, le=12)
    minimum_shot_duration_seconds: int = Field(default=5, ge=2, le=12)
    minimum_source_video_coverage: float = Field(default=0.70, ge=0.20, le=1.0)
    max_source_asset_share: float = Field(default=0.45, ge=0.20, le=1.0)
    allow_generated_connective_visuals: bool = True
    exclude_people: bool = True
    vision_required: bool = False


class PostPrimerBridgeDefinition(BaseModel):
    """Moderator hand-off from an approved topic primer into the panel."""

    editorial_brief: str = ""
    target_duration_seconds: int = Field(default=35, ge=15, le=45)
    introduce_participants: bool | None = None


class OpeningDefinition(BaseModel):
    """Evidence-led topic primer that precedes the panel discussion."""

    enabled: bool = True
    narration_brief: str = ""
    source_references: list[str] = Field(default_factory=list)
    # Retained for compatibility with episode definitions created before the
    # bridge received its own explicit configuration.
    introduce_participants: bool = False
    post_primer_bridge: PostPrimerBridgeDefinition = Field(
        default_factory=PostPrimerBridgeDefinition
    )
    narrator_profile_id: str | None = None
    target_duration_seconds: int = Field(default=60, ge=15, le=180)
    media_discovery: PrimerMediaDiscoveryDefinition = Field(
        default_factory=PrimerMediaDiscoveryDefinition
    )
    visual_planner: PrimerVisualPlannerDefinition = Field(
        default_factory=PrimerVisualPlannerDefinition
    )


class PrimerMediaCandidate(BaseModel):
    id: str = Field(min_length=1)
    media_url: str = Field(min_length=1, max_length=2048)
    source_url: str = Field(min_length=1, max_length=2048)
    source_title: str = Field(min_length=1, max_length=512)
    source_type: str = Field(min_length=1, max_length=128)
    media_type: Literal["image", "video"]
    title: str = Field(min_length=1, max_length=512)
    rationale: str = Field(min_length=1, max_length=1200)
    rights_status: Literal["official_source", "editorial_review_required", "unknown"]
    acquisition_method: Literal["direct", "platform_video"] = "direct"
    confidence: float = Field(default=0.5, ge=0, le=1)
    status: Literal["proposed", "acquired", "rejected", "failed"] = "proposed"
    asset_id: UUID | None = None
    failure_reason: str | None = None


class PrimerMediaDiscoveryRequest(BaseModel):
    user_id: str | None = None


class PrimerMediaAcquireRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = None


class PrimerMediaImportRequest(BaseModel):
    """Import operator-selected public media directly into the private primer source library."""

    source_url: str = Field(min_length=8, max_length=2048)
    title: str = Field(default="", max_length=256)
    user_id: str | None = None


class PrimerVisualPlanPrepareRequest(BaseModel):
    """Draft a narration script and a producer-reviewable source-media plan."""

    user_id: str | None = None
    reuse_existing_script: bool = False
    replace_existing_plan: bool = False
    script_override: str | None = Field(default=None, min_length=40, max_length=12_000)


class PrimerNarrationTimingRequest(BaseModel):
    """Generate or reuse the approved narrator WAV before timing visual edits."""

    user_id: str | None = None
    regenerate: bool = False


class PrimerVisualPlanBeatUpdateRequest(BaseModel):
    asset_id: UUID
    source_start_ms: int = Field(default=0, ge=0)
    source_end_ms: int | None = Field(default=None, ge=1)
    user_id: str | None = None

    @model_validator(mode="after")
    def source_range_is_valid(self) -> PrimerVisualPlanBeatUpdateRequest:
        if self.source_end_ms is not None and self.source_end_ms <= self.source_start_ms:
            raise ValueError("source_end_ms must be greater than source_start_ms")
        return self


class PrimerVisualPlanBeatCreateRequest(BaseModel):
    """Insert an unassigned visual beat into an existing reviewed plan."""

    after_beat_id: str | None = Field(default=None, min_length=1, max_length=128)
    asset_id: UUID | None = None
    user_id: str | None = None


class PrimerVisualPlanApprovalRequest(BaseModel):
    user_id: str | None = None


class PrimerVisualPlanVerificationRequest(BaseModel):
    """Re-check manually selected source-video excerpts without rebuilding the storyboard."""

    user_id: str | None = None


class PrimerVisualPlanRevisionRestoreRequest(BaseModel):
    user_id: str | None = None


class PrimerVisualPlanRevisionSummary(BaseModel):
    id: str
    created_at: str
    reason: str
    actor: str
    status: str
    beat_count: int


class PrimerVisualPlanRevisionList(BaseModel):
    episode_id: UUID
    revisions: list[PrimerVisualPlanRevisionSummary] = Field(default_factory=list)


class PrimerVisualPlanStatus(BaseModel):
    episode_id: UUID
    status: Literal["not_prepared", "review_required", "blocked", "approved"] = "not_prepared"
    script: str = ""
    script_checksum: str | None = None
    beat_count: int = 0
    video_beat_count: int = 0
    distinct_video_asset_count: int = 0
    video_coverage_ratio: float = 0.0
    coverage: dict[str, Any] = Field(default_factory=dict)
    beats: list[dict[str, Any]] = Field(default_factory=list)
    planner: dict[str, Any] | None = None
    narration_timing: dict[str, Any] | None = None
    failure: str | None = None


class PrimerSpokenScriptReplacement(BaseModel):
    source: str = Field(min_length=1, max_length=256)
    spoken: str = Field(min_length=1, max_length=256)
    category: Literal["acronym", "name", "number", "unit", "symbol", "custom"] = "custom"
    origin: Literal["deterministic", "dictionary", "ai", "editor"] = "deterministic"
    reason: str | None = Field(default=None, max_length=500)


class PrimerSpokenScriptPrepareRequest(BaseModel):
    user_id: str | None = None


class PrimerSpokenScriptUpdateRequest(BaseModel):
    replacements: list[PrimerSpokenScriptReplacement] = Field(
        default_factory=list,
        max_length=128,
    )
    punctuation_script: str | None = Field(default=None, min_length=1, max_length=12_000)
    user_id: str | None = None


class PrimerSpokenScriptApprovalRequest(BaseModel):
    user_id: str | None = None


class PrimerSpokenScriptStatus(BaseModel):
    status: Literal[
        "not_required",
        "not_prepared",
        "review_required",
        "approved",
        "outdated",
        "blocked",
    ] = "not_prepared"
    editorial_script: str = ""
    editorial_script_checksum: str | None = None
    spoken_script: str = ""
    spoken_script_checksum: str | None = None
    profile_fingerprint: str | None = None
    replacements: list[PrimerSpokenScriptReplacement] = Field(default_factory=list)
    ai_assistance: dict[str, Any] | None = None
    prepared_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    failure: str | None = None


class PrimerProductionRequest(BaseModel):
    """Create an off-camera, evidence-led topic primer for review."""

    user_id: str | None = None
    regenerate: bool = False
    reuse_existing_script: bool = False
    regenerate_narration: bool = False
    rebuild_visual_plan: bool = False
    replace_existing_visual_plan: bool = False
    script_override: str | None = Field(default=None, min_length=40, max_length=12_000)


class PrimerProductionStatus(BaseModel):
    episode_id: UUID
    status: str
    narrator_profile_id: str | None = None
    target_duration_seconds: int = 0
    script: str = ""
    source_count: int = 0
    media_asset_count: int = 0
    narration_asset_id: UUID | None = None
    actual_narration_duration_ms: int | None = None
    narration_timing: dict[str, Any] | None = None
    timeline_asset_id: UUID | None = None
    render_asset_id: UUID | None = None
    render_download_url: str | None = None
    editorial_polish: dict[str, Any] | None = None
    narration_quality: dict[str, Any] | None = None
    spoken_script: PrimerSpokenScriptStatus | None = None
    visual_plan: PrimerVisualPlanStatus | None = None
    failure: str | None = None


class MediaDirectingDefinition(BaseModel):
    """Episode-scoped camera policy for the rendered talk-show timeline.

    Speaker mouth animation remains single-person and audio driven. Group and
    reaction footage is deliberately silent visual coverage around those shots.
    """

    mode: Literal["studio_directed", "speaker_only"] = "studio_directed"
    planning_mode: Literal["auto_with_overrides", "manual"] = "auto_with_overrides"
    # The seated-panel path is deliberately an episode decision. Participant
    # profiles remain reusable characters; episode assignments decide where
    # they sit and how the current discussion is photographed.
    studio_layout: Literal["seated_panel", "legacy_overlay"] = "legacy_overlay"
    seating_plan: dict[str, int] = Field(default_factory=dict)
    broll_presentation: Literal["wall_screen_only", "disabled"] = "wall_screen_only"
    require_generated_studio: bool = True
    require_group_cutaways: bool = True
    require_reaction_cutaways: bool = True
    broll_policy: Literal["contextual_only", "disabled"] = "contextual_only"
    default_camera_views: list[
        Literal[
            "establishing_wide",
            "panel_two_shot",
            "speaker_medium",
            "speaker_close_up",
            "reaction",
            "contextual_broll",
        ]
    ] = Field(
        default_factory=lambda: [
            "establishing_wide",
            "panel_two_shot",
            "speaker_medium",
            "speaker_close_up",
            "reaction",
            "contextual_broll",
        ]
    )
    allowed_camera_actions: list[
        Literal[
            "cut",
            "dissolve",
            "slow_push",
            "slow_pull",
            "fly_in",
            "pan_left",
            "pan_right",
        ]
    ] = Field(
        default_factory=lambda: [
            "cut",
            "dissolve",
            "slow_push",
            "slow_pull",
            "fly_in",
            "pan_left",
            "pan_right",
        ]
    )

    @model_validator(mode="after")
    def studio_requirements_are_coherent(self) -> MediaDirectingDefinition:
        if self.mode == "speaker_only":
            self.require_generated_studio = False
            self.require_group_cutaways = False
            self.require_reaction_cutaways = False
            self.broll_presentation = "disabled"
        seats = list(self.seating_plan.values())
        duplicate_seats = {seat for seat in seats if seats.count(seat) > 1}
        if duplicate_seats:
            raise ValueError("each configured studio seat must belong to one participant")
        if any(seat < 1 for seat in self.seating_plan.values()):
            raise ValueError("studio seat numbers must start at 1")
        return self


class MediaDefinition(BaseModel):
    aspect_ratio: str = "16:9"
    width: int = Field(default=1920, ge=320)
    height: int = Field(default=1080, ge=240)
    fps: int = Field(default=30, ge=1, le=120)
    visual_style: str = "studio_realistic"
    camera_style: str = "multi_camera"
    scene_reference_image_uri: str | None = None
    subtitle_mode: str = "selectable"
    generate_broll: bool = True
    generate_citation_cards: bool = True
    evidence_presentation: Literal["review_only", "burned_overlays"] = "review_only"
    opening: OpeningDefinition = Field(default_factory=OpeningDefinition)
    directing: MediaDirectingDefinition = Field(default_factory=MediaDirectingDefinition)


class WorkflowDefinition(BaseModel):
    mode: str = "transcript_review"
    production_target: Literal["native_visual", "audio_first"] = "native_visual"
    retry_failed_assets: bool = True
    maximum_stage_retries: int = Field(default=3, ge=0, le=10)


class QualityDefinition(BaseModel):
    block_on_unsupported_high_impact_claims: bool = True
    block_on_missing_audio: bool = True
    block_on_sync_error_ms: int = Field(default=180, ge=0)
    block_on_missing_subtitles: bool = False


class EpisodeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3)
    topic: TopicDefinition
    format: FormatDefinition = Field(default_factory=FormatDefinition)
    duration_allocation: DurationAllocation = Field(default_factory=DurationAllocation)
    languages: LanguageDefinition = Field(default_factory=LanguageDefinition)
    participants: list[ParticipantAssignment]
    research: ResearchDefinition = Field(default_factory=ResearchDefinition)
    media: MediaDefinition = Field(default_factory=MediaDefinition)
    workflow: WorkflowDefinition = Field(default_factory=WorkflowDefinition)
    quality: QualityDefinition = Field(default_factory=QualityDefinition)

    @model_validator(mode="after")
    def participant_requirements(self) -> EpisodeDefinition:
        host_count = sum(1 for item in self.participants if item.role == "moderator")
        if host_count != 1:
            raise ValueError("episode definition must include exactly one moderator")
        if len(self.participants) < 4:
            raise ValueError("increment 1 requires one host and at least three participants")
        if self.format.participant_count != len(self.participants):
            raise ValueError("format participant_count must match participant assignments")
        return self


class Project(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=256)
    description: str = ""
    default_language: str = Field(default="en", min_length=2, max_length=12)
    default_show_format_id: str = "analytical_panel_v1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = ""
    default_language: str = Field(default="en", min_length=2, max_length=12)
    default_show_format_id: str = "analytical_panel_v1"

    def to_project(self, project_id: UUID | None = None) -> Project:
        payload = self.model_dump()
        if project_id is not None:
            payload["id"] = project_id
        return Project(**payload)


class LanguageProfile(BaseModel):
    id: str
    name: str
    bcp47_tag: str = Field(min_length=2, max_length=12)
    native_name: str = ""
    default_mode: str = "localized_reperformance"
    subtitle_direction: str = "ltr"
    line_breaking: dict[str, Any] = Field(default_factory=dict)
    voice_defaults: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class LanguageProfileCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    bcp47_tag: str = Field(min_length=2, max_length=12)
    native_name: str = ""
    default_mode: str = "localized_reperformance"
    subtitle_direction: str = "ltr"
    line_breaking: dict[str, Any] = Field(default_factory=dict)
    voice_defaults: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    def to_profile(self) -> LanguageProfile:
        return LanguageProfile(**self.model_dump())


class SamplingSettings(BaseModel):
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.95, ge=0, le=1)
    max_tokens: int = Field(default=500, ge=1, le=8000)


class ModelEndpoint(BaseUrlWithoutUserInfo):
    id: str
    name: str
    provider_type: ProviderType = ProviderType.mock
    base_url: str | None = None
    credential_reference: str | None = None
    default_timeout_seconds: int = Field(default=60, ge=1)
    max_concurrency: int = Field(default=2, ge=1)
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"


class ModelEndpointCreateRequest(BaseUrlWithoutUserInfo):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    provider_type: ProviderType = ProviderType.mock
    base_url: str | None = None
    credential_reference: str | None = None
    default_timeout_seconds: int = Field(default=60, ge=1)
    max_concurrency: int = Field(default=2, ge=1)
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"

    def to_endpoint(self) -> ModelEndpoint:
        return ModelEndpoint(**self.model_dump())


class VoiceboxEndpoint(BaseUrlWithoutUserInfo):
    id: str
    name: str
    adapter_type: str = "voicebox_http"
    base_url: str | None = None
    credential_reference: str | None = None
    default_timeout_seconds: int = Field(default=60, ge=1)
    max_concurrency: int = Field(default=2, ge=1)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 3})
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"


class VoiceboxEndpointCreateRequest(BaseUrlWithoutUserInfo):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    adapter_type: str = "voicebox_http"
    base_url: str | None = None
    credential_reference: str | None = None
    default_timeout_seconds: int = Field(default=60, ge=1)
    max_concurrency: int = Field(default=2, ge=1)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 3})
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"

    def to_endpoint(self) -> VoiceboxEndpoint:
        return VoiceboxEndpoint(**self.model_dump())


class VoiceProfile(BaseModel):
    id: str
    name: str
    voicebox_endpoint_id: str
    voice_id: str
    language: str = Field(min_length=2, max_length=12)
    speaker_label: str | None = None
    model_id: str | None = None
    prosody: dict[str, Any] = Field(default_factory=dict)
    rate: float = Field(default=1.0, gt=0)
    pitch: float = 0
    pronunciation_dictionary: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class VoiceProfileCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    voicebox_endpoint_id: str = Field(min_length=1, max_length=128)
    voice_id: str = Field(min_length=1, max_length=256)
    language: str = Field(default="en", min_length=2, max_length=12)
    speaker_label: str | None = None
    model_id: str | None = None
    prosody: dict[str, Any] = Field(default_factory=dict)
    rate: float = Field(default=1.0, gt=0)
    pitch: float = 0
    pronunciation_dictionary: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    def to_profile(self) -> VoiceProfile:
        return VoiceProfile(**self.model_dump())


class VoicePreviewRequest(BaseModel):
    """Ephemeral voice synthesis request used by the configuration UI."""

    profile: VoiceProfileCreateRequest
    text: str = Field(
        default="Die wichtigsten Fakten zuerst. Ich spreche klar, ruhig und gut verstaendlich.",
        min_length=1,
        max_length=600,
    )


class B1VoicePresetProvisionRequest(BaseModel):
    assign_participants: bool = True
    reassign_participants: bool = False


class B1VoicePresetProvisionResponse(BaseModel):
    voicebox_endpoint_id: str
    created_voice_profile_ids: list[str] = Field(default_factory=list)
    existing_voice_profile_ids: list[str] = Field(default_factory=list)
    assigned_participants: dict[str, str] = Field(default_factory=dict)
    preserved_assigned_participant_ids: list[str] = Field(default_factory=list)
    reassigned_participant_ids: list[str] = Field(default_factory=list)
    reassign_participants: bool = False


class B1VoiceInventorySyncResponse(BaseModel):
    voicebox_endpoint_id: str
    discovered_voice_count: int = 0
    created_voice_profile_ids: list[str] = Field(default_factory=list)
    existing_voice_profile_ids: list[str] = Field(default_factory=list)
    skipped_voice_ids: list[str] = Field(default_factory=list)


class OpenRouterPresetProvisionRequest(BaseModel):
    assign_participants: bool = True


class OpenRouterPresetProvisionResponse(BaseModel):
    model_endpoint_id: str
    created_endpoint: bool = False
    updated_endpoint: bool = False
    assigned_participants: dict[str, str] = Field(default_factory=dict)
    missing_participant_ids: list[str] = Field(default_factory=list)


class ComfyUiEndpoint(BaseUrlWithoutUserInfo):
    id: str
    name: str
    adapter_type: str = "comfyui_http"
    base_url: str | None = None
    credential_reference: str | None = None
    default_timeout_seconds: int = Field(default=120, ge=1)
    max_concurrency: int = Field(default=1, ge=1)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 2})
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"


class ComfyUiEndpointCreateRequest(BaseUrlWithoutUserInfo):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    adapter_type: str = "comfyui_http"
    base_url: str | None = None
    credential_reference: str | None = None
    default_timeout_seconds: int = Field(default=120, ge=1)
    max_concurrency: int = Field(default=1, ge=1)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 2})
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"

    def to_endpoint(self) -> ComfyUiEndpoint:
        return ComfyUiEndpoint(**self.model_dump())


class ComfyUiWorkflow(BaseModel):
    id: str
    name: str
    workflow_type: str
    version: str = "v1"
    comfyui_endpoint_id: str
    output_asset_type: AssetType = AssetType.video
    api_workflow: dict[str, Any] = Field(default_factory=dict)
    prompt_template: dict[str, Any] = Field(default_factory=dict)
    default_parameters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ComfyUiWorkflowCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    workflow_type: str = Field(min_length=1, max_length=128)
    version: str = Field(default="v1", min_length=1, max_length=64)
    comfyui_endpoint_id: str = Field(min_length=1, max_length=128)
    output_asset_type: AssetType = AssetType.video
    api_workflow: dict[str, Any] = Field(default_factory=dict)
    prompt_template: dict[str, Any] = Field(default_factory=dict)
    default_parameters: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    def to_workflow(self) -> ComfyUiWorkflow:
        return ComfyUiWorkflow(**self.model_dump())


class DiscussionPromptTemplate(BaseModel):
    id: str
    version: str = "1.0.0"
    participant_type: ParticipantType
    system: str
    user: str
    variables: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    enabled: bool = True
    change_summary: str = ""


class DiscussionPromptTemplateCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    participant_type: ParticipantType
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(default="operator", min_length=1, max_length=256)
    created_at: datetime | None = None
    enabled: bool = True
    change_summary: str = Field(default="", max_length=1024)

    def to_template(self) -> DiscussionPromptTemplate:
        payload = self.model_dump()
        payload["created_at"] = payload["created_at"] or datetime.now(UTC)
        return DiscussionPromptTemplate(**payload)


VisualReferenceImageType = Literal["portrait", "full_body", "wardrobe"]


class VisualReferenceImage(BaseModel):
    reference_type: VisualReferenceImageType
    uri: str = Field(min_length=1)
    filename: str | None = None
    content_type: str | None = None
    checksum: str | None = None
    size_bytes: int | None = None
    uploaded_at: datetime | None = None


class CharacterPerformance(BaseModel):
    on_camera_energy: Literal["restrained", "measured", "engaged"] = "measured"
    gaze_style: Literal["steady", "reflective", "responsive"] = "steady"
    head_motion: Literal["minimal", "subtle", "expressive"] = "subtle"
    expression_range: Literal["contained", "warm", "animated"] = "contained"
    gesture_frequency: Literal["none", "occasional", "frequent"] = "occasional"
    signature_habit: str = Field(default="", max_length=256)
    variation_seed: int | None = None


class VisualProfile(BaseModel):
    id: str
    name: str
    character_name: str
    primary_workflow_id: str
    reaction_workflow_id: str | None = None
    broll_workflow_id: str | None = None
    reference_image_uri: str | None = None
    reference_images: list[VisualReferenceImage] = Field(default_factory=list)
    style_prompt: str = ""
    negative_prompt: str = ""
    seed: int | None = None
    wardrobe: dict[str, Any] = Field(default_factory=dict)
    performance: CharacterPerformance = Field(default_factory=CharacterPerformance)
    enabled: bool = True


class VisualProfileCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    character_name: str = Field(min_length=1, max_length=256)
    primary_workflow_id: str = Field(min_length=1, max_length=128)
    reaction_workflow_id: str | None = None
    broll_workflow_id: str | None = None
    reference_image_uri: str | None = None
    reference_images: list[VisualReferenceImage] = Field(default_factory=list)
    style_prompt: str = ""
    negative_prompt: str = ""
    seed: int | None = None
    wardrobe: dict[str, Any] = Field(default_factory=dict)
    performance: CharacterPerformance = Field(default_factory=CharacterPerformance)
    enabled: bool = True

    def to_profile(self) -> VisualProfile:
        return VisualProfile(**self.model_dump())


class VisualProfileReferenceImageUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)
    image_base64: str = Field(min_length=1)
    reference_type: VisualReferenceImageType = "portrait"
    user_id: str | None = None


class SceneReferenceImageUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)
    image_base64: str = Field(min_length=1)
    user_id: str | None = None


class SceneReferenceImageUploadResponse(BaseModel):
    scene_reference_image_uri: str
    content_type: str
    checksum: str
    size_bytes: int
    object_key: str


class OpeningMediaUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)
    media_base64: str = Field(min_length=1)
    title: str = Field(default="", max_length=256)
    source_url: str | None = Field(default=None, max_length=2048)
    user_id: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str | None = None
    user_id: str | None = None


class TurnReviewActionRequest(BaseModel):
    comment: str | None = None
    user_id: str | None = None


class TurnManualEditRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    comment: str | None = Field(default=None, max_length=2000)
    user_id: str | None = None


class AssetReplacementRequest(BaseModel):
    storage_uri: str = Field(min_length=1)
    mime_type: str | None = None
    checksum: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    fps: float | None = Field(default=None, gt=0)
    status: str = Field(default="completed", pattern="^(completed|ready)$")
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    user_id: str | None = None
    comment: str | None = None


class LocalizationRequest(BaseModel):
    languages: list[str] | None = None
    user_id: str | None = None
    regenerate: bool = False


class AudioAssetPlanRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    user_id: str | None = None
    regenerate: bool = False


class AudioGenerationRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    user_id: str | None = None
    regenerate: bool = False


class AudioQualityRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    user_id: str | None = None


class AudioResultSyncRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    include_completed: bool = False
    user_id: str | None = None


class AudioCancellationRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    reset_to_planned: bool = True
    user_id: str | None = None


class VisualAssetPlanRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    user_id: str | None = None
    regenerate: bool = False


class VisualGenerationRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    user_id: str | None = None
    regenerate: bool = False
    fallback_on_failure: bool = True
    local_fallback_only: bool = False


class StudioPanelReviewRequest(BaseModel):
    """Human decision for the shared studio master before turn clips use it."""

    decision: Literal["approved", "rejected"]
    comment: str | None = None
    user_id: str | None = None


class SeatedCharacterReviewRequest(BaseModel):
    """Human decision for a generated seated-character plate."""

    decision: Literal["approved", "rejected"]
    comment: str | None = None
    user_id: str | None = None


class VisualResultSyncRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    include_completed: bool = False
    user_id: str | None = None
    fallback_on_failure: bool = True


class VisualQualityRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    user_id: str | None = None


class VisualCancellationRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    asset_ids: list[UUID] | None = None
    transcript_turn_ids: list[UUID] | None = None
    participant_ids: list[str] | None = None
    failed_only: bool = False
    reset_to_planned: bool = True
    user_id: str | None = None


class SubtitleGenerationRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    format: str = Field(default="vtt", pattern="^(vtt|srt)$")
    user_id: str | None = None
    regenerate: bool = False


class TimelineBuildRequest(BaseModel):
    transcript_version_id: UUID | None = None
    language: str | None = None
    user_id: str | None = None
    regenerate: bool = False


class TimelineUpdateRequest(BaseModel):
    timeline: dict[str, Any]
    user_id: str | None = None
    comment: str | None = None


class EpisodeTimeline(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    language: str
    version: int = Field(default=1, ge=1)
    status: str = "completed"
    duration_ms: int = Field(default=0, ge=0)
    timeline_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RenderPreset(BaseModel):
    id: str
    name: str
    width: int = Field(ge=320)
    height: int = Field(ge=240)
    fps: int = Field(ge=1, le=120)
    video_bitrate: str
    audio_bitrate: str = "160k"
    audio_sample_rate: int = Field(default=48_000, ge=8000)
    audio_layout: str = "stereo"
    pixel_format: str = "yuv420p"
    container: str = "mp4"
    codec: str = "libx264"
    render_scope: str = "video"
    enabled: bool = True


class RenderRequest(BaseModel):
    timeline_asset_id: UUID | None = None
    transcript_version_id: UUID | None = None
    language: str | None = None
    render_type: str = Field(default="preview", pattern="^(preview|final)$")
    review_scope: Literal["full_timeline", "qualification_slice"] = "full_timeline"
    preset_id: str = "preview-low-bitrate"
    user_id: str | None = None
    regenerate: bool = False
    allow_unapproved_preview: bool = False
    allow_paused_episode: bool = False


class ThumbnailRequest(BaseModel):
    render_asset_id: UUID | None = None
    user_id: str | None = None
    regenerate: bool = False


class YouTubeExportRequest(BaseModel):
    render_asset_id: UUID | None = None
    thumbnail_asset_id: UUID | None = None
    user_id: str | None = None
    regenerate: bool = False
    allow_preview_render: bool = False


class ProductionManifestRequest(BaseModel):
    package_asset_id: UUID | None = None
    render_asset_id: UUID | None = None
    user_id: str | None = None
    regenerate: bool = False


class PublisherTarget(BaseUrlWithoutUserInfo):
    id: str
    name: str
    platform: str = Field(default="youtube", pattern="^(youtube|generic)$")
    adapter_type: str = "mock"
    base_url: str | None = None
    credential_reference: str | None = None
    channel_id: str | None = None
    privacy_status: str = Field(default="unlisted", pattern="^(private|unlisted|public)$")
    default_language: str = Field(default="en", min_length=2, max_length=12)
    default_tags: list[str] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 2})
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"


class PublisherTargetCreateRequest(BaseUrlWithoutUserInfo):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    platform: str = Field(default="youtube", pattern="^(youtube|generic)$")
    adapter_type: str = "mock"
    base_url: str | None = None
    credential_reference: str | None = None
    channel_id: str | None = None
    privacy_status: str = Field(default="unlisted", pattern="^(private|unlisted|public)$")
    default_language: str = Field(default="en", min_length=2, max_length=12)
    default_tags: list[str] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 2})
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)
    health_status: str = "unknown"

    def to_target(self) -> PublisherTarget:
        return PublisherTarget(**self.model_dump())


class PublishRequest(BaseModel):
    publisher_target_id: str = "mock-youtube"
    package_asset_id: UUID | None = None
    user_id: str | None = None
    dry_run: bool = True
    regenerate: bool = False


class WorkflowActionRequest(BaseModel):
    action: str = Field(
        pattern=(
            "^(pause|resume|cancel|stop_run|retry_failed_stage|approve_stage|reject_stage|"
            "continue_after_manual_edit|complete)$"
        )
    )
    user_id: str | None = None
    comment: str | None = None


class WorkflowStartRequest(BaseModel):
    user_id: str | None = None
    comment: str | None = Field(default=None, max_length=512)


class WorkflowRunUntilBlockedRequest(BaseModel):
    start_if_needed: bool = True
    max_passes: int = Field(default=4, ge=1, le=10)
    user_id: str | None = None
    comment: str | None = Field(default=None, max_length=512)


class WorkflowRunUntilBlockedResponse(BaseModel):
    episode: Episode
    status: str
    stop_reason: str
    pass_count: int
    progressed_stage_count: int
    handoff: dict[str, Any] | None = None
    summaries: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[Approval] = Field(default_factory=list)
    completion_readiness: dict[str, Any] = Field(default_factory=dict)


class WorkflowRetryResolutionRequest(BaseModel):
    user_id: str | None = None
    comment: str | None = Field(default=None, max_length=512)


class WorkerHeartbeatRequest(BaseModel):
    role: str = Field(min_length=1, max_length=128)
    worker_id: str = Field(min_length=1, max_length=256)
    status: str = Field(default="running", pattern="^(starting|running|idle|degraded|failed)$")
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerSignalRequest(BaseModel):
    target_role: str = Field(min_length=1, max_length=128)
    signal_type: str = Field(pattern="^(drain|resume|reload|stop_after_current)$")
    reason: str | None = Field(default=None, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict)
    user_id: str | None = Field(default=None, max_length=256)


class LiveProviderCastPreflightRequest(BaseModel):
    participant_ids: list[str] = Field(default_factory=list)
    frontier_cast: bool = True
    include_models: bool = True
    include_voices: bool = True
    text: str = Field(
        default="Guten Tag. DialectiCore prueft jetzt eine echte Stimme fuer den Pilottest.",
        min_length=1,
        max_length=1000,
    )
    user_id: str | None = Field(default=None, max_length=256)


class B1ManagedMediaSmokeRequest(BaseModel):
    api_base: str = Field(default="https://api.ai.b1.germering", max_length=512)
    model: str = Field(default="image-default", min_length=1, max_length=128)
    prompt: str = Field(
        default="small neutral studio lighting test card, no text",
        min_length=1,
        max_length=1000,
    )
    negative_prompt: str = Field(default="text, watermark, logo", max_length=1000)
    width: int = Field(default=128, ge=64, le=2048)
    height: int = Field(default=128, ge=64, le=2048)
    steps: int = Field(default=1, ge=1, le=40)
    cfg: float = Field(default=1.0, ge=0, le=20)
    seed: int = Field(default=7, ge=0)
    poll_attempts: int = Field(default=12, ge=1, le=60)
    poll_interval_seconds: float = Field(default=10.0, ge=0, le=60)
    evidence_output: str | None = Field(default=None, max_length=512)
    requirements_output: str | None = Field(
        default="/home/mordred/media-requirements.md",
        max_length=512,
    )
    allow_runner_failure: bool = False
    user_id: str | None = Field(default=None, max_length=256)


class WorkerStatusRecord(BaseModel):
    role: str
    worker_id: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: datetime
    last_heartbeat_at: datetime
    heartbeat_age_seconds: float = Field(default=0, ge=0)
    stale: bool = False


class WorkerLeaseRecord(BaseModel):
    role: str
    worker_id: str
    acquired_at: datetime
    last_renewed_at: datetime
    expires_at: datetime
    lease_age_seconds: float = Field(default=0, ge=0)
    expires_in_seconds: float = 0
    expired: bool = False


class WorkerStatusSummary(BaseModel):
    status: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    heartbeat_ttl_seconds: int
    lease_ttl_seconds: int = Field(default=0, ge=0)
    runtime_state_retention_seconds: int = Field(default=0, ge=0)
    workers: list[WorkerStatusRecord] = Field(default_factory=list)
    leases: list[WorkerLeaseRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class BackupCreateRequest(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    user_id: str | None = None
    include_object_storage: bool = True
    include_runtime_state: bool = True


class BackupRestoreRequest(BaseModel):
    backup_path: str = Field(min_length=1)
    apply: bool = False
    restore_database: bool = True
    restore_object_storage: bool = True
    restore_runtime_state: bool = False
    replace_existing: bool = True
    user_id: str | None = None


class ProviderSessionRevocationRequest(BaseModel):
    token_sha256: str | None = Field(default=None, min_length=64, max_length=71)
    jti: str | None = Field(default=None, min_length=1, max_length=256)
    subject: str | None = Field(default=None, min_length=1, max_length=512)
    expires_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=256)
    user_id: str | None = None

    @model_validator(mode="after")
    def at_least_one_identifier(self) -> ProviderSessionRevocationRequest:
        if not (self.token_sha256 or self.jti or self.subject):
            raise ValueError("revocation requires token_sha256, jti, or subject")
        return self


class ParticipantProfile(BaseModel):
    id: str
    name: str
    display_name: str
    participant_type: ParticipantType
    model_endpoint_id: str
    model_id: str
    system_prompt_template: str
    perspective: str
    expertise: str
    speaking_style: str
    sampling_settings: SamplingSettings = Field(default_factory=SamplingSettings)
    tool_policy_id: str = "no_tools"
    voice_profile_id: str | None = None
    visual_profile_id: str | None = None
    enabled: bool = True


class ParticipantProfileCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    participant_type: ParticipantType
    model_endpoint_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    system_prompt_template: str = Field(min_length=1)
    perspective: str = Field(min_length=1)
    expertise: str = Field(min_length=1)
    speaking_style: str = Field(min_length=1)
    sampling_settings: SamplingSettings = Field(default_factory=SamplingSettings)
    tool_policy_id: str = "no_tools"
    voice_profile_id: str | None = None
    visual_profile_id: str | None = None
    enabled: bool = True

    def to_profile(self) -> ParticipantProfile:
        return ParticipantProfile(**self.model_dump())


class PronunciationDictionaryEntry(BaseModel):
    source: str = Field(min_length=1, max_length=256)
    spoken: str = Field(min_length=1, max_length=256)
    category: Literal["acronym", "name", "number", "unit", "symbol", "custom"] = "custom"
    case_sensitive: bool = False


class PrimerPronunciationSettings(BaseModel):
    enabled: bool = False
    use_ai: bool = True
    model_endpoint_id: str | None = Field(default=None, max_length=128)
    model_id: str | None = Field(default=None, max_length=256)
    strictness: Literal["conservative", "balanced"] = "conservative"
    acronym_policy: Literal["spell_out", "expand_known", "preserve"] = "spell_out"
    expand_numbers: bool = True
    expand_units: bool = True
    optimize_pauses: bool = True
    require_review: bool = True
    custom_dictionary: list[PronunciationDictionaryEntry] = Field(
        default_factory=list,
        max_length=128,
    )


class PrimerNarratorProfile(BaseModel):
    """Reusable off-camera narrator. It is intentionally not a cast participant."""

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    language: str = Field(default="de", min_length=2, max_length=12)
    model_endpoint_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    voice_profile_id: str = Field(min_length=1, max_length=128)
    delivery_rate: float = Field(
        default=0.9,
        ge=0.7,
        le=1.25,
        description="Pitch-preserving narration pace, where 1.0 is the native voice pace.",
    )
    editorial_style: str = Field(
        default="Clear, concise and neutral. Explain evidence without taking a panel position.",
        min_length=1,
        max_length=2000,
    )
    sampling_settings: SamplingSettings = Field(default_factory=SamplingSettings)
    pronunciation: PrimerPronunciationSettings = Field(default_factory=PrimerPronunciationSettings)
    enabled: bool = True


class PrimerNarratorProfileCreateRequest(PrimerNarratorProfile):
    def to_profile(self) -> PrimerNarratorProfile:
        return PrimerNarratorProfile(**self.model_dump())


class Claim(BaseModel):
    text: str
    claim_type: str = "opinion"
    confidence: float = Field(default=0.5, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)


class EvidenceSource(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    uri: str | None = None
    author: str | None = None
    published_at: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float = Field(default=0.5, ge=0, le=1)
    content_checksum: str | None = None
    score_factors: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""


class ResearchSource(BaseModel):
    id: str = Field(min_length=1)
    episode_id: UUID
    url: str | None = None
    title: str = Field(min_length=1)
    publisher: str | None = None
    published_at: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_hash: str | None = None
    source_type: str = Field(min_length=1)
    credibility_score: float = Field(default=0.5, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceClaim(BaseModel):
    id: str = Field(min_length=1)
    episode_id: UUID | None = None
    statement: str | None = None
    text: str = Field(min_length=1)
    claim_type: str = "context"
    confidence: float = Field(default=0.5, ge=0, le=1)
    status: Literal[
        "verified",
        "supported",
        "uncertain",
        "opinion",
        "prediction",
        "unsupported",
        "contradicted",
    ] = "supported"
    supporting_source_ids: list[str] = Field(default_factory=list)
    contradicting_source_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    contradicting_evidence_refs: list[str] = Field(default_factory=list)
    uncertainty: str | None = None
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)


class EvidencePack(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    schema_version: str = "evidence_pack.v1"
    episode_id: UUID
    topic: TopicDefinition
    research_depth: str = "standard"
    sub_questions: list[str] = Field(default_factory=list)
    definitions: list[EvidenceClaim] = Field(default_factory=list)
    verified_facts: list[EvidenceClaim] = Field(default_factory=list)
    supported_claims: list[EvidenceClaim] = Field(default_factory=list)
    uncertain_claims: list[EvidenceClaim] = Field(default_factory=list)
    disputed_claims: list[EvidenceClaim] = Field(default_factory=list)
    competing_interpretations: list[EvidenceClaim] = Field(default_factory=list)
    important_statistics: list[EvidenceClaim] = Field(default_factory=list)
    source_index: list[EvidenceSource] = Field(default_factory=list)
    source_rankings: list[dict[str, Any]] = Field(default_factory=list)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    source_reviews: list[dict[str, Any]] = Field(default_factory=list)
    source_agreements: list[dict[str, Any]] = Field(default_factory=list)
    source_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    cross_source_summary: dict[str, Any] = Field(default_factory=dict)
    suggested_discussion_dimensions: list[str] = Field(default_factory=list)
    fact_check_rules: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchSourceInput(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1)
    uri: str | None = None
    source_type: str = "manual_source"
    author: str | None = None
    published_at: str | None = None
    retrieved_at: datetime | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    content: str = Field(default="", max_length=20000)
    summary: str = ""


class ResearchRetrievalTarget(BaseModel):
    title: str | None = None
    uri: str = Field(min_length=1)
    source_type: str = "web_page"
    author: str | None = None
    published_at: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    summary: str = ""
    discovered_by: str | None = None
    discovery_query: str | None = None
    discovery_rank: int | None = Field(default=None, ge=1)


class ResearchBuildRequest(BaseModel):
    user_id: str | None = None
    regenerate: bool = False
    require_approval: bool | None = None
    sources: list[ResearchSourceInput] = Field(default_factory=list)
    retrieval_targets: list[ResearchRetrievalTarget] = Field(default_factory=list)
    discover_sources: bool = False
    discovery_queries: list[str] = Field(default_factory=list)


class ResearchClaimQcRequest(BaseModel):
    transcript_version_id: UUID | None = None
    evidence_pack_asset_id: UUID | None = None
    user_id: str | None = None


class ResearchSourceReviewRequest(BaseModel):
    source_id: str = Field(min_length=1)
    decision: Literal["approved", "rejected", "needs_revision"]
    evidence_pack_asset_id: UUID | None = None
    user_id: str | None = None
    notes: str = ""


class PrivateMemoryUpdate(BaseModel):
    unspoken_points: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    position_summary: str = ""


class StructuredTurnOutput(BaseModel):
    spoken_text: str = Field(min_length=1)
    intent: str
    responding_to: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    questions_for_others: list[dict[str, str]] = Field(default_factory=list)
    requested_follow_up: bool = False
    private_memory_update: PrivateMemoryUpdate = Field(default_factory=PrivateMemoryUpdate)


class ParticipantMemory(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    discussion_session_id: UUID | None = None
    participant_id: str
    version: int = 1
    private_notes: list[str] = Field(default_factory=list)
    unspoken_points: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    position_summary: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DiscussionTurn(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    discussion_session_id: UUID | None = None
    sequence_number: int
    speaker_participant_id: str
    turn_type: TurnType
    spoken_text: str
    intent: str
    responding_to_turn_id: UUID | None = None
    estimated_duration_seconds: float
    actual_audio_duration_seconds: float | None = None
    structured_output: StructuredTurnOutput
    raw_provider_response: dict[str, Any]
    generation_metadata: dict[str, Any]
    status: str = "accepted"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SpeakerBalance(BaseModel):
    total_turns: int = 0
    total_words: int = 0
    estimated_speaking_seconds: float = 0
    actual_speaking_seconds: float = 0
    unanswered_questions: int = 0
    recency_of_last_turn: int | None = None


class DiscussionSession(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    phase: str = "introduction"
    status: str = "pending"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    estimated_duration_seconds: float = 0
    coverage_state: dict[str, bool] = Field(default_factory=dict)
    speaker_balance_state: dict[str, SpeakerBalance] = Field(default_factory=dict)
    host_state: dict[str, Any] = Field(default_factory=dict)
    controller_state: dict[str, Any] = Field(default_factory=dict)
    turns: list[DiscussionTurn] = Field(default_factory=list)
    memories: dict[str, ParticipantMemory] = Field(default_factory=dict)


class TranscriptTurn(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    transcript_version_id: UUID | None = None
    source_discussion_turn_ids: list[UUID]
    speaker_participant_id: str
    turn_type: TurnType | None = None
    text: str
    edit_type: str = "verbatim"
    semantic_difference_score: float = 0
    claims: list[Claim] = Field(default_factory=list)
    pronunciation_markup: str | None = None
    status: str = "pending_review"


class TranscriptVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    type: TranscriptType
    language: str
    parent_version_id: UUID | None = None
    status: str = "pending_review"
    semantic_fidelity_score: float = 1
    localization_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    turns: list[TranscriptTurn] = Field(default_factory=list)


class Asset(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    asset_type: AssetType
    language: str | None = None
    source_entity_type: str
    source_entity_id: str
    storage_uri: str | None = None
    mime_type: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    checksum: str | None = None
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "planned"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EpisodeMediaAsset(BaseModel):
    """Compact playable-media projection for the episode production surface."""

    id: UUID
    asset_type: AssetType
    source_entity_type: str
    source_entity_id: str
    storage_uri: str | None = None
    mime_type: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    status: str
    character_name: str | None = None
    visual_role: str | None = None
    performance_applied: bool = False


class QualityResult(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    target_type: str
    target_id: str
    check_type: str
    severity: QualitySeverity
    status: str
    score: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Approval(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    stage: str
    target_type: str | None = None
    target_id: str | None = None
    decision: str = "pending"
    comment: str | None = None
    user_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID | None = None
    event_type: str
    actor: str = "system"
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PublishJob(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    publisher_target_id: str
    platform: str
    package_asset_id: UUID
    status: str = Field(default="submitted", pattern="^(submitted|completed|failed|replaced)$")
    dry_run: bool = True
    remote_job_id: str | None = None
    publish_url: str | None = None
    delivery_payload: dict[str, Any] = Field(default_factory=dict)
    result_metadata: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Episode(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID | None = None
    title: str
    slug: str
    subject: str
    central_question: str
    status: EpisodeStatus = EpisodeStatus.draft
    source_language: str = "en"
    target_duration_seconds: int
    minimum_duration_seconds: int
    maximum_duration_seconds: int
    current_workflow_id: str | None = None
    canonical_transcript_version_id: UUID | None = None
    workflow_control: dict[str, Any] = Field(default_factory=dict)
    definition: EpisodeDefinition
    participants: list[ParticipantProfile]
    model_endpoints: list[ModelEndpoint]
    discussion_session: DiscussionSession | None = None
    transcripts: list[TranscriptVersion] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    quality_results: list[QualityResult] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    publish_jobs: list[PublishJob] = Field(default_factory=list)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EpisodeSummary(BaseModel):
    id: UUID
    project_id: UUID | None = None
    title: str
    slug: str
    status: EpisodeStatus
    source_language: str
    target_duration_seconds: int
    minimum_duration_seconds: int
    maximum_duration_seconds: int
    current_workflow_id: str | None = None
    canonical_transcript_version_id: UUID | None = None
    output_languages: list[str] = Field(default_factory=list)
    discussion_phase: str | None = None
    discussion_status: str | None = None
    discussion_turn_count: int = 0
    estimated_duration_seconds: float = 0
    transcript_count: int = 0
    asset_count: int = 0
    quality_result_count: int = 0
    publish_job_count: int = 0
    pending_approval_count: int = 0
    pending_approvals: list[Approval] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class WorkflowAdvanceResponse(BaseModel):
    episode: Episode
    summary: dict[str, Any]


class EpisodeCreateRequest(BaseModel):
    project_id: UUID | None = None
    definition: EpisodeDefinition
    participants: list[ParticipantProfile] | None = None
    model_endpoints: list[ModelEndpoint] | None = None


class EpisodeDefinitionUpdateRequest(EpisodeCreateRequest):
    user_id: str | None = None


class EpisodeProductionSettingsUpdateRequest(BaseModel):
    target_duration_seconds: int = Field(ge=1)
    minimum_duration_seconds: int = Field(ge=1)
    maximum_duration_seconds: int = Field(ge=1)
    scene_reference_image_uri: str | None = None
    opening: OpeningDefinition | None = None
    directing: MediaDirectingDefinition | None = None
    user_id: str | None = None


class ProductionStatus(BaseModel):
    episode_id: UUID
    status: EpisodeStatus
    current_stage: str
    workflow_paused: bool = False
    workflow_cancelled: bool = False
    retry_available: bool = False
    workflow_control: dict[str, Any] = Field(default_factory=dict)
    current_discussion_phase: str | None = None
    turn_count: int = 0
    estimated_duration_seconds: float = 0
    target_duration_seconds: int
    speaker_balance: dict[str, SpeakerBalance] = Field(default_factory=dict)
    awaiting_approval: bool = False
