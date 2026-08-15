from enum import StrEnum


class EpisodeStatus(StrEnum):
    draft = "DRAFT"
    validating = "VALIDATING"
    researching = "RESEARCHING"
    research_review = "RESEARCH_REVIEW"
    preparing_discussion = "PREPARING_DISCUSSION"
    discussing = "DISCUSSING"
    transcript_qc = "TRANSCRIPT_QC"
    transcript_review = "TRANSCRIPT_REVIEW"
    localizing = "LOCALIZING"
    generating_audio = "GENERATING_AUDIO"
    generating_visuals = "GENERATING_VISUALS"
    building_timeline = "BUILDING_TIMELINE"
    rendering_preview = "RENDERING_PREVIEW"
    preview_qc = "PREVIEW_QC"
    preview_review = "PREVIEW_REVIEW"
    rendering_final = "RENDERING_FINAL"
    final_qc = "FINAL_QC"
    ready = "READY"
    exporting = "EXPORTING"
    completed = "COMPLETED"
    failed = "FAILED"
    cancelled = "CANCELLED"


class ParticipantType(StrEnum):
    host = "host"
    panelist = "panelist"
    fact_checker = "fact_checker"
    guest = "guest"
    audience_proxy = "audience_proxy"


class TurnType(StrEnum):
    host_opening = "host_opening"
    post_primer_bridge = "post_primer_bridge"
    question = "question"
    opening_position = "opening_position"
    rebuttal = "rebuttal"
    clarification = "clarification"
    closing_statement = "closing_statement"
    host_synthesis = "host_synthesis"


class TranscriptType(StrEnum):
    raw = "raw"
    broadcast = "broadcast"
    localized = "localized"


class QualitySeverity(StrEnum):
    pass_ = "pass"
    warning = "warning"
    fail = "fail"


class ProviderType(StrEnum):
    mock = "mock"
    openai_compatible = "openai_compatible"
    ollama = "ollama"
    anthropic_compatible = "anthropic_compatible"
    mistral_compatible = "mistral_compatible"
    generic_http = "generic_http"


class AssetType(StrEnum):
    audio = "audio"
    image = "image"
    video = "video"
    subtitle = "subtitle"
    evidence_pack = "evidence_pack"
    timeline = "timeline"
    render = "render"
    thumbnail = "thumbnail"
    export_package = "export_package"
    production_manifest = "production_manifest"
    citation_card = "citation_card"
    broll = "broll"
    reaction_loop = "reaction_loop"
    studio_scene = "studio_scene"
