import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from app.core.credentials import normalize_credential_reference
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DIALECTICORE_", env_file=".env", extra="ignore")

    env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    cors_allowed_origins: str = "*"
    database_url: str | None = "sqlite:///./dialecticore-dev.db"
    database_driver: str = "postgresql+psycopg"
    database_host: str = "localhost"
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = "dialecticore"
    database_user: str = "dialecticore"
    database_password: str | None = None
    database_password_reference: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    redis_event_fanout_enabled: bool = False
    redis_event_channel: str = "dialecticore:system-events"
    redis_worker_signal_enabled: bool = False
    redis_worker_signal_stream: str = "dialecticore:worker-signals"
    redis_worker_signal_maxlen: int = Field(default=200, ge=1)
    redis_timeout_seconds: float = Field(default=1.0, gt=0)
    auth_enabled: bool = False
    auth_api_key_reference: str | None = None
    auth_api_key_header: str = "x-dialecticore-api-key"
    auth_role_header: str = "x-dialecticore-role"
    auth_user_header: str = "x-dialecticore-user"
    auth_trusted_identity_enabled: bool = False
    auth_trusted_identity_header: str = "x-forwarded-user"
    auth_trusted_email_header: str = "x-forwarded-email"
    auth_trusted_groups_header: str = "x-forwarded-groups"
    auth_trusted_group_role_map: str = ""
    auth_trusted_default_role: str = "viewer"
    auth_provider_session_enabled: bool = False
    auth_provider_session_introspection_url: str | None = None
    auth_provider_session_client_id_reference: str | None = None
    auth_provider_session_client_secret_reference: str | None = None
    auth_provider_session_token_header: str = "authorization"
    auth_provider_session_user_claim: str = "sub"
    auth_provider_session_groups_claim: str = "groups"
    auth_provider_session_group_role_map: str = ""
    auth_provider_session_default_role: str = "viewer"
    auth_provider_session_timeout_seconds: float = Field(default=3.0, gt=0)
    auth_provider_session_revocation_path: str | None = None
    auth_provider_session_decision_log_path: str | None = None
    auth_provider_session_decision_log_limit: int = Field(default=100, ge=1)
    object_storage_backend: str = "local"
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_bucket: str = "dialecticore"
    object_storage_local_path: str = "./storage/object-store"
    object_storage_region: str = "us-east-1"
    object_storage_access_key_reference: str | None = None
    object_storage_secret_key_reference: str | None = None
    object_storage_force_path_style: bool = True
    object_storage_auto_create_bucket: bool = True
    runtime_state_path: str = "./storage/runtime-state"
    backup_path: str = "./storage/backups"
    audio_loudness_target_lufs: float = -16.0
    audio_loudness_true_peak_limit_dbtp: float = -1.5
    audio_loudness_range_target_lu: float = 11.0
    model_provider: str = "mock"
    words_per_second: float = Field(default=2.45, gt=0)
    discussion_duration_audio_safety_factor: float = Field(default=0.9, gt=0, le=1)
    worker_poll_interval_seconds: int = Field(default=15, ge=1)
    worker_snapshot_refresh_seconds: int = Field(default=30, ge=5)
    worker_sync_batch_limit: int = Field(default=50, ge=1)
    worker_heartbeat_ttl_seconds: int = Field(default=90, ge=1)
    worker_lease_ttl_seconds: int = Field(default=45, ge=1)
    worker_runtime_state_retention_seconds: int = Field(default=86_400, ge=1)
    worker_required_roles: str = "workflow-worker"
    worker_auto_start_production_runs_enabled: bool = False
    b1_managed_media_smoke_evidence_path: str = (
        "output/smoke/b1-managed-media-smoke-image-default-latest.json"
    )
    runtime_path_min_free_bytes: int = Field(default=0, ge=0)
    temporal_signal_transport_enabled: bool = False
    temporal_signal_endpoint: str | None = None
    temporal_signal_timeout_seconds: float = Field(default=3.0, gt=0)
    temporal_namespace: str = "default"
    temporal_task_queue: str | None = None
    temporal_backend_mode: str = "local"
    temporal_backend_address: str | None = None
    temporal_backend_tls_enabled: bool = False
    temporal_backend_worker_enabled: bool = False
    temporal_backend_connect_timeout_seconds: float = Field(default=1.0, gt=0)
    workflow_stage_retry_max_attempts: int = Field(default=3, ge=1)
    workflow_stage_retry_backoff_seconds: int = Field(default=60, ge=1)
    publisher_automated_live_enabled: bool = False
    research_retrieval_timeout_seconds: int = Field(default=8, ge=1)
    research_retrieval_max_bytes: int = Field(default=1_000_000, ge=1024)
    research_discovery_enabled: bool = False
    research_discovery_url_template: str | None = None
    research_discovery_max_queries: int = Field(default=4, ge=1)
    research_discovery_max_results_per_query: int = Field(default=5, ge=1)
    research_advanced_extraction_enabled: bool = False
    research_advanced_extraction_url: str | None = None
    research_advanced_extraction_timeout_seconds: int = Field(default=15, ge=1)
    research_advanced_extraction_max_sources: int = Field(default=8, ge=1)
    research_advanced_extraction_max_claims_per_source: int = Field(default=6, ge=1)
    primer_media_source_timeout_seconds: int = Field(default=8, ge=1)
    primer_media_source_max_bytes: int = Field(default=1_000_000, ge=1024)
    primer_media_download_timeout_seconds: int = Field(default=90, ge=1)
    primer_media_max_download_bytes: int = Field(default=384 * 1024 * 1024, ge=1024)
    primer_media_web_search_enabled: bool = True
    primer_media_web_search_max_queries: int = Field(default=3, ge=1, le=6)
    primer_media_web_search_max_results_per_query: int = Field(default=8, ge=1, le=20)
    primer_media_platform_search_enabled: bool = True
    primer_media_platform_search_results_per_query: int = Field(default=3, ge=1, le=8)
    primer_visual_planner_default_endpoint_id: str | None = "openrouter"
    primer_visual_planner_default_model_id: str | None = "google/gemini-3.6-flash"
    primer_visual_planner_default_shot_duration_seconds: int = Field(default=6, ge=2, le=12)
    primer_visual_planner_default_source_video_coverage: float = Field(
        default=0.70, ge=0.20, le=1.0
    )

    def resolved_database_url(self) -> str:
        configured = (self.database_url or "").strip()
        if configured:
            return configured
        driver = self.database_driver.strip()
        if driver.startswith("sqlite"):
            return f"{driver}:///{self.database_name}"
        password = self.database_password
        if password is None and self.database_password_reference:
            password = self._resolve_secret_reference(self.database_password_reference)
        username = quote(self.database_user, safe="")
        auth = username
        if password is not None:
            auth = f"{username}:{quote(password, safe='')}"
        host = self.database_host.strip()
        database = quote(self.database_name, safe="")
        return f"{driver}://{auth}@{host}:{self.database_port}/{database}"

    def resolved_cors_allowed_origins(self) -> list[str]:
        origins = [
            origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()
        ]
        return origins or ["*"]

    def _resolve_secret_reference(self, reference: str) -> str:
        try:
            reference = normalize_credential_reference(reference) or ""
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if reference.startswith("env:"):
            env_name = reference.removeprefix("env:")
            value = os.getenv(env_name)
            if not value:
                raise RuntimeError("credential reference is not available")
            return value
        if reference.startswith("file:"):
            path = Path(reference.removeprefix("file:"))
            if not path.is_absolute():
                raise RuntimeError("file credential reference must use an absolute path")
            return self._read_secret_file(path, reference)
        if reference.startswith("docker-secret:"):
            secret_name = reference.removeprefix("docker-secret:")
            if (
                not secret_name
                or "/" in secret_name
                or "\\" in secret_name
                or secret_name in {".", ".."}
            ):
                raise RuntimeError("docker-secret credential reference is invalid")
            return self._read_secret_file(Path("/run/secrets") / secret_name, reference)
        raise RuntimeError("unsupported credential reference scheme")

    def _read_secret_file(self, path: Path, reference: str) -> str:
        try:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError("credential reference is not available") from exc
        if not value:
            raise RuntimeError("credential reference is not available")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
