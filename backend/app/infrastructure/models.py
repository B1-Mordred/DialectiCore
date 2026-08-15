from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "project_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    default_language: Mapped[str] = mapped_column(String(12), nullable=False)
    default_show_format_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LanguageProfileRecord(Base):
    __tablename__ = "language_profile_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    bcp47_tag: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    default_mode: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subtitle_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EpisodeRecord(Base):
    __tablename__ = "episode_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_language: Mapped[str] = mapped_column(String(12), nullable=False)
    target_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    current_workflow_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_transcript_version_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchSourceRecord(Base):
    __tablename__ = "research_source_records"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    credibility_score: Mapped[float] = mapped_column(nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvidenceClaimRecord(Base):
    __tablename__ = "evidence_claim_records"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    statement: Mapped[str] = mapped_column(String(4096), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssetRecord(Base):
    __tablename__ = "asset_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    source_entity_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    storage_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelEndpointRecord(Base):
    __tablename__ = "model_endpoint_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    credential_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    health_status: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ParticipantProfileRecord(Base):
    __tablename__ = "participant_profile_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    participant_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VoiceboxEndpointRecord(Base):
    __tablename__ = "voicebox_endpoint_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    credential_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    health_status: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VoiceProfileRecord(Base):
    __tablename__ = "voice_profile_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    voicebox_endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    voice_id: Mapped[str] = mapped_column(String(256), nullable=False)
    language: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PrimerNarratorProfileRecord(Base):
    __tablename__ = "primer_narrator_profile_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    language: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    model_endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(256), nullable=False)
    voice_profile_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ComfyUiEndpointRecord(Base):
    __tablename__ = "comfyui_endpoint_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    credential_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    health_status: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ComfyUiWorkflowRecord(Base):
    __tablename__ = "comfyui_workflow_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    comfyui_endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    output_asset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscussionPromptTemplateRecord(Base):
    __tablename__ = "discussion_prompt_template_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    participant_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VisualProfileRecord(Base):
    __tablename__ = "visual_profile_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    character_name: Mapped[str] = mapped_column(String(256), nullable=False)
    primary_workflow_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublisherTargetRecord(Base):
    __tablename__ = "publisher_target_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    credential_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    health_status: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventRecord(Base):
    __tablename__ = "audit_event_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    episode_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
