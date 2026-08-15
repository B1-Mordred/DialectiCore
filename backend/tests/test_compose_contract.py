import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from app.services.worker_status_service import WORKER_ROLES
from app.workflows.worker_placeholder import supported_worker_roles

ROOT = Path(__file__).resolve().parents[2]
WORKER_SERVICE_ROLES = [
    "workflow-worker",
    "temporal-worker",
    "discussion-worker",
    "research-worker",
    "localization-worker",
    "voicebox-adapter",
    "comfyui-adapter",
    "timeline-worker",
    "render-worker",
    "qc-worker",
    "publishing-worker",
]
MEDIA_WRITING_WORKER_ROLES = {
    "workflow-worker",
    "temporal-worker",
    "research-worker",
    "voicebox-adapter",
    "comfyui-adapter",
    "timeline-worker",
    "render-worker",
    "qc-worker",
    "publishing-worker",
}
RUNTIME_ONLY_WORKER_ROLES = {"discussion-worker", "localization-worker"}
NORMAL_WORKER_ROLES = [
    role for role in WORKER_SERVICE_ROLES if role != "temporal-worker"
]
INFRASTRUCTURE_SERVICE_ROLES = [
    "temporal",
    "temporal-ui",
    "postgres",
    "redis",
    "minio",
]
DATA_PLANE_SERVICE_ROLES = [
    "postgres",
    "redis",
    "minio",
]
EDGE_SERVICE_ROLES = [
    "web-ui",
    "production-api",
    "temporal-ui",
]
BACKEND_SERVICE_ROLES = [
    "production-api",
    "temporal",
    "temporal-ui",
    *WORKER_SERVICE_ROLES,
    *DATA_PLANE_SERVICE_ROLES,
]
RUNTIME_HARDENING_MERGE = "<<: [*bounded-json-logging, *process-resource-limits]"
INFRASTRUCTURE_RUNTIME_HARDENING = "<<: *infrastructure-runtime-hardening"


def _service_block(compose_text: str, service_name: str) -> str:
    services_start = compose_text.index("services:\n")
    match = re.search(rf"^  {re.escape(service_name)}:\n", compose_text[services_start:], re.M)
    if match is None:
        raise AssertionError(f"service {service_name} not found")
    start = services_start + match.start()
    next_match = re.search(r"^  [A-Za-z0-9_-]+:\n", compose_text[start + 1 :], re.M)
    if next_match is None:
        return compose_text[start:]
    next_service = start + 1 + next_match.start()
    return compose_text[start:next_service]


def _compose_config(
    *files: str,
    profiles: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is not available")
    result = subprocess.run(
        [
            "docker",
            "compose",
            *[
                item
                for filename in files
                for item in ("-f", str(ROOT / filename))
            ],
            *[item for profile in profiles for item in ("--profile", profile)],
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env=None if env is None else {**os.environ, **env},
    )
    return json.loads(result.stdout)


def test_compose_worker_services_match_runtime_worker_role_registries() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert WORKER_SERVICE_ROLES == WORKER_ROLES
    assert sorted(WORKER_SERVICE_ROLES) == sorted(supported_worker_roles())

    for role in WORKER_SERVICE_ROLES:
        block = _service_block(compose_text, role)
        assert "dockerfile: Dockerfile.worker" in block
        assert f"DIALECTICORE_WORKER_ROLE: {role}" in block
        assert "healthcheck: *worker-healthcheck" in block


def test_default_compose_file_renders_with_expected_runtime_services() -> None:
    config = _compose_config("docker-compose.yml")
    services = config["services"]

    assert set(NORMAL_WORKER_ROLES).issubset(services)
    assert "temporal-worker" not in services
    assert {"web-ui", "production-api", "postgres", "redis", "minio", "temporal"}.issubset(
        services
    )
    assert services["postgres"]["image"] == "postgres:16"
    assert services["redis"]["image"] == "redis:7-alpine"
    assert services["minio"]["image"] == "minio/minio:RELEASE.2025-07-23T15-54-02Z"
    assert services["temporal"]["image"] == "temporalio/auto-setup:1.25"
    assert services["production-api"]["environment"]["DIALECTICORE_DATABASE_URL"] == (
        "postgresql+psycopg://dialecticore:dialecticore@postgres:5432/dialecticore"
    )
    assert (
        services["production-api"]["environment"]["DIALECTICORE_DATABASE_DRIVER"]
        == "postgresql+psycopg"
    )
    assert services["production-api"]["environment"]["DIALECTICORE_DATABASE_HOST"] == "postgres"
    assert services["production-api"]["environment"]["DIALECTICORE_DATABASE_PORT"] == "5432"
    assert (
        services["production-api"]["environment"]["DIALECTICORE_DATABASE_NAME"]
        == "dialecticore"
    )
    assert (
        services["production-api"]["environment"]["DIALECTICORE_DATABASE_USER"]
        == "dialecticore"
    )
    assert (
        services["production-api"]["environment"][
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE"
        ]
        == ""
    )
    assert services["production-api"]["environment"]["DIALECTICORE_REDIS_URL"] == (
        "redis://redis:6379/0"
    )
    assert services["production-api"]["environment"][
        "DIALECTICORE_AUTH_API_KEY_REFERENCE"
    ] == "env:DIALECTICORE_API_KEY"
    assert services["production-api"]["environment"][
        "DIALECTICORE_AUTH_API_KEY_HEADER"
    ] == "x-dialecticore-api-key"
    assert services["production-api"]["environment"]["DIALECTICORE_AUTH_ROLE_HEADER"] == (
        "x-dialecticore-role"
    )
    assert services["production-api"]["environment"]["DIALECTICORE_AUTH_USER_HEADER"] == (
        "x-dialecticore-user"
    )
    assert services["postgres"]["healthcheck"]["interval"] == "10s"
    assert services["postgres"]["healthcheck"]["timeout"] == "5s"
    assert services["postgres"]["healthcheck"]["retries"] == 5
    assert services["redis"]["healthcheck"]["interval"] == "10s"
    assert services["redis"]["healthcheck"]["timeout"] == "5s"
    assert services["redis"]["healthcheck"]["retries"] == 5
    assert services["minio"]["healthcheck"]["interval"] == "10s"
    assert services["minio"]["healthcheck"]["timeout"] == "5s"
    assert services["minio"]["healthcheck"]["retries"] == 12
    assert services["minio"]["healthcheck"]["start_period"] == "10s"
    assert services["temporal"]["healthcheck"]["interval"] == "10s"
    assert services["temporal"]["healthcheck"]["timeout"] == "6s"
    assert services["temporal"]["healthcheck"]["retries"] == 12
    assert services["temporal"]["healthcheck"]["start_period"] == "20s"
    assert services["production-api"]["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "app.healthcheck",
    ]
    assert services["production-api"]["healthcheck"]["interval"] == "10s"
    assert services["production-api"]["healthcheck"]["timeout"] == "6s"
    assert services["production-api"]["healthcheck"]["retries"] == 12
    assert services["production-api"]["healthcheck"]["start_period"] == "20s"
    assert services["production-api"]["environment"][
        "DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES"
    ] == "healthy,degraded"
    assert services["production-api"]["environment"]["DIALECTICORE_HEALTHCHECK_URL"] == (
        "http://127.0.0.1:8000/api/v1/system/health"
    )
    assert services["production-api"]["environment"][
        "DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS"
    ] == "5"
    assert services["production-api"]["environment"][
        "DIALECTICORE_AUDIO_LOUDNESS_TARGET_LUFS"
    ] == "-16"
    assert services["production-api"]["environment"][
        "DIALECTICORE_AUDIO_LOUDNESS_TRUE_PEAK_LIMIT_DBTP"
    ] == "-1.5"
    assert services["production-api"]["environment"][
        "DIALECTICORE_AUDIO_LOUDNESS_RANGE_TARGET_LU"
    ] == "11"
    assert services["production-api"]["environment"]["DIALECTICORE_MODEL_PROVIDER"] == (
        "mock"
    )
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_BACKEND"
    ] == "s3"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_ENDPOINT"
    ] == "http://minio:9000"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_BUCKET"
    ] == "dialecticore"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH"
    ] == "/data/object-storage"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_REGION"
    ] == "us-east-1"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE"
    ] == "env:MINIO_ROOT_USER"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE"
    ] == "env:MINIO_ROOT_PASSWORD"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_FORCE_PATH_STYLE"
    ] == "true"
    assert services["production-api"]["environment"][
        "DIALECTICORE_OBJECT_STORAGE_AUTO_CREATE_BUCKET"
    ] == "true"
    assert services["web-ui"]["healthcheck"]["interval"] == "10s"
    assert services["web-ui"]["healthcheck"]["timeout"] == "5s"
    assert services["web-ui"]["healthcheck"]["retries"] == 6
    assert services["web-ui"]["healthcheck"]["start_period"] == "10s"
    assert services["web-ui"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["production-api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["minio"]["ports"][0]["host_ip"] == "127.0.0.1"
    for role in NORMAL_WORKER_ROLES:
        assert services[role]["environment"]["DIALECTICORE_WORKER_ROLE"] == role
        assert services[role]["healthcheck"]["test"] == [
            "CMD",
            "python",
            "-m",
            "app.worker_healthcheck",
        ]
        assert services[role]["healthcheck"]["interval"] == "30s"
        assert services[role]["healthcheck"]["timeout"] == "6s"
        assert services[role]["healthcheck"]["retries"] == 3
        assert services[role]["healthcheck"]["start_period"] == "30s"
    profiled_config = _compose_config("docker-compose.yml", profiles=("temporal-external",))
    assert profiled_config["services"]["temporal-worker"]["environment"][
        "DIALECTICORE_WORKER_ROLE"
    ] == "temporal-worker"


def test_ops_ui_compose_profile_renders_loopback_temporal_ui() -> None:
    config = _compose_config("docker-compose.yml", profiles=("ops-ui",))
    services = config["services"]
    temporal_ui = services["temporal-ui"]

    assert temporal_ui["profiles"] == ["ops-ui"]
    assert temporal_ui["environment"]["TEMPORAL_ADDRESS"] == "temporal:7233"
    assert temporal_ui["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8080,
            "published": "8088",
            "protocol": "tcp",
        }
    ]
    assert temporal_ui["image"] == "temporalio/ui:2.31.2"
    assert set(temporal_ui["networks"]) == {"edge", "backend"}
    assert temporal_ui["depends_on"] == {
        "temporal": {"condition": "service_healthy", "required": True}
    }
    assert temporal_ui["security_opt"] == ["no-new-privileges:true"]


def test_production_secret_compose_overlay_renders_secret_backed_app_config() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        profiles=("temporal-external",),
    )
    services = config["services"]
    app_services = ["production-api", *WORKER_SERVICE_ROLES]

    for service_name in app_services:
        service = services[service_name]
        environment = service["environment"]
        assert environment["DIALECTICORE_ENV"] == "production"
        assert environment["DIALECTICORE_AUTH_ENABLED"] == "true"
        assert environment["DIALECTICORE_AUTH_API_KEY_REFERENCE"] == (
            "docker-secret:dialecticore_api_key"
        )
        assert environment["DIALECTICORE_AUTH_API_KEY_HEADER"] == (
            "x-dialecticore-api-key"
        )
        assert environment["DIALECTICORE_AUTH_ROLE_HEADER"] == "x-dialecticore-role"
        assert environment["DIALECTICORE_AUTH_USER_HEADER"] == "x-dialecticore-user"
        assert environment["DIALECTICORE_DATABASE_URL"] == ""
        assert environment["DIALECTICORE_DATABASE_DRIVER"] == "postgresql+psycopg"
        assert environment["DIALECTICORE_DATABASE_HOST"] == "postgres"
        assert environment["DIALECTICORE_DATABASE_PORT"] == "5432"
        assert environment["DIALECTICORE_DATABASE_NAME"] == "dialecticore"
        assert environment["DIALECTICORE_DATABASE_USER"] == "dialecticore"
        assert environment["DIALECTICORE_DATABASE_PASSWORD_REFERENCE"] == (
            "docker-secret:postgres_password"
        )
        assert environment["DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE"] == (
            "docker-secret:minio_root_password"
        )
        assert environment["DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE"] == (
            "docker-secret:minio_root_user"
        )
        assert environment["DIALECTICORE_REDIS_EVENT_FANOUT_ENABLED"] == "true"
        assert environment["DIALECTICORE_REDIS_WORKER_SIGNAL_ENABLED"] == "true"
        assert environment["DIALECTICORE_REDIS_URL"] == "redis://redis:6379/0"
        assert environment["DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES"] == (
            "healthy,degraded"
        )
        assert environment["DIALECTICORE_HEALTHCHECK_URL"] == (
            "http://127.0.0.1:8000/api/v1/system/health"
        )
        assert environment["DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS"] == "5"
        assert environment["DIALECTICORE_AUDIO_LOUDNESS_TARGET_LUFS"] == "-16"
        assert environment["DIALECTICORE_AUDIO_LOUDNESS_TRUE_PEAK_LIMIT_DBTP"] == "-1.5"
        assert environment["DIALECTICORE_AUDIO_LOUDNESS_RANGE_TARGET_LU"] == "11"
        assert environment["DIALECTICORE_MODEL_PROVIDER"] == "mock"
        assert environment["DIALECTICORE_OBJECT_STORAGE_BACKEND"] == "s3"
        assert environment["DIALECTICORE_OBJECT_STORAGE_ENDPOINT"] == "http://minio:9000"
        assert environment["DIALECTICORE_OBJECT_STORAGE_BUCKET"] == "dialecticore"
        assert environment["DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH"] == (
            "/data/object-storage"
        )
        assert environment["DIALECTICORE_OBJECT_STORAGE_REGION"] == "us-east-1"
        assert environment["DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE"] == (
            "docker-secret:minio_root_user"
        )
        assert environment["DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE"] == (
            "docker-secret:minio_root_password"
        )
        assert environment["DIALECTICORE_OBJECT_STORAGE_FORCE_PATH_STYLE"] == "true"
        assert environment["DIALECTICORE_OBJECT_STORAGE_AUTO_CREATE_BUCKET"] == "true"
        assert environment["DIALECTICORE_API_KEY"] == ""
        assert environment["B1_API_KEY"] == ""
        assert environment["B1_API_KEY_FILE"] == "/run/secrets/b1_api_key"
        assert environment["OPENROUTER_API_KEY"] == ""
        assert environment["OPENROUTER_API_KEY_FILE"] == (
            "/run/secrets/openrouter_api_key"
        )
        assert environment["MINIO_ROOT_USER"] == ""
        assert environment["MINIO_ROOT_PASSWORD"] == ""
        assert {
            secret["source"]: secret["mode"]
            for secret in service.get("secrets", [])
        } == {
            "dialecticore_api_key": "0444",
            "postgres_password": "0444",
            "minio_root_user": "0444",
            "minio_root_password": "0444",
            "b1_api_key": "0444",
            "openrouter_api_key": "0444",
        }

    assert "ports" not in services["production-api"]
    assert "ports" not in services["minio"]
    assert services["web-ui"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/postgres_password"
    )
    assert services["minio"]["environment"]["MINIO_ROOT_PASSWORD_FILE"] == (
        "/run/secrets/minio_root_password"
    )
    assert services["minio"]["environment"]["MINIO_ROOT_USER_FILE"] == (
        "/run/secrets/minio_root_user"
    )


def test_compose_audio_loudness_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_AUDIO_LOUDNESS_TARGET_LUFS": "-18",
            "DIALECTICORE_AUDIO_LOUDNESS_TRUE_PEAK_LIMIT_DBTP": "-2",
            "DIALECTICORE_AUDIO_LOUDNESS_RANGE_TARGET_LU": "9",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_AUDIO_LOUDNESS_TARGET_LUFS"] == "-18"
    assert (
        production_environment["DIALECTICORE_AUDIO_LOUDNESS_TRUE_PEAK_LIMIT_DBTP"]
        == "-2"
    )
    assert production_environment["DIALECTICORE_AUDIO_LOUDNESS_RANGE_TARGET_LU"] == "9"


def test_compose_object_storage_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_OBJECT_STORAGE_BACKEND": "s3",
            "DIALECTICORE_OBJECT_STORAGE_ENDPOINT": "https://s3.example.test",
            "DIALECTICORE_OBJECT_STORAGE_BUCKET": "dialecticore-prod",
            "DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH": "/data/probe-cache",
            "DIALECTICORE_OBJECT_STORAGE_REGION": "eu-central-1",
            "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE": (
                "file:/run/private/s3-access-key"
            ),
            "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE": (
                "file:/run/private/s3-secret-key"
            ),
            "DIALECTICORE_OBJECT_STORAGE_FORCE_PATH_STYLE": "false",
            "DIALECTICORE_OBJECT_STORAGE_AUTO_CREATE_BUCKET": "false",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_OBJECT_STORAGE_BACKEND"] == "s3"
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_ENDPOINT"]
        == "https://s3.example.test"
    )
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_BUCKET"]
        == "dialecticore-prod"
    )
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH"]
        == "/data/probe-cache"
    )
    assert production_environment["DIALECTICORE_OBJECT_STORAGE_REGION"] == "eu-central-1"
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE"]
        == "file:/run/private/s3-access-key"
    )
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE"]
        == "file:/run/private/s3-secret-key"
    )
    assert production_environment["DIALECTICORE_OBJECT_STORAGE_FORCE_PATH_STYLE"] == "false"
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_AUTO_CREATE_BUCKET"]
        == "false"
    )


def test_production_secrets_object_storage_credential_references_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        env={
            "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE": (
                "docker-secret:external_s3_access_key"
            ),
            "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE": (
                "docker-secret:external_s3_secret_key"
            ),
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE"]
        == "docker-secret:external_s3_access_key"
    )
    assert (
        production_environment["DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE"]
        == "docker-secret:external_s3_secret_key"
    )


def test_compose_database_settings_are_operator_tunable() -> None:
    full_url_config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_DATABASE_URL": (
                "postgresql+psycopg://dialecticore@db.example.test:5432/prod"
            ),
        },
    )
    assert full_url_config["services"]["production-api"]["environment"][
        "DIALECTICORE_DATABASE_URL"
    ] == "postgresql+psycopg://dialecticore@db.example.test:5432/prod"

    assembled_config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_DATABASE_URL": "",
            "DIALECTICORE_DATABASE_DRIVER": "postgresql+psycopg",
            "DIALECTICORE_DATABASE_HOST": "db.internal",
            "DIALECTICORE_DATABASE_PORT": "6543",
            "DIALECTICORE_DATABASE_NAME": "dialecticore_prod",
            "DIALECTICORE_DATABASE_USER": "dialecticore_app",
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE": "file:/run/private/db-pass",
        },
    )
    production_environment = assembled_config["services"]["production-api"][
        "environment"
    ]

    assert production_environment["DIALECTICORE_DATABASE_URL"] == ""
    assert production_environment["DIALECTICORE_DATABASE_HOST"] == "db.internal"
    assert production_environment["DIALECTICORE_DATABASE_PORT"] == "6543"
    assert production_environment["DIALECTICORE_DATABASE_NAME"] == "dialecticore_prod"
    assert production_environment["DIALECTICORE_DATABASE_USER"] == "dialecticore_app"
    assert (
        production_environment["DIALECTICORE_DATABASE_PASSWORD_REFERENCE"]
        == "file:/run/private/db-pass"
    )


def test_production_secrets_database_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        env={
            "DIALECTICORE_DATABASE_DRIVER": "postgresql+psycopg",
            "DIALECTICORE_DATABASE_HOST": "db.prod.internal",
            "DIALECTICORE_DATABASE_PORT": "6432",
            "DIALECTICORE_DATABASE_NAME": "dialecticore_prod",
            "DIALECTICORE_DATABASE_USER": "dialecticore_api",
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE": "docker-secret:external_db_pass",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_DATABASE_URL"] == ""
    assert production_environment["DIALECTICORE_DATABASE_HOST"] == "db.prod.internal"
    assert production_environment["DIALECTICORE_DATABASE_PORT"] == "6432"
    assert production_environment["DIALECTICORE_DATABASE_NAME"] == "dialecticore_prod"
    assert production_environment["DIALECTICORE_DATABASE_USER"] == "dialecticore_api"
    assert (
        production_environment["DIALECTICORE_DATABASE_PASSWORD_REFERENCE"]
        == "docker-secret:external_db_pass"
    )


def test_compose_redis_url_is_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={"DIALECTICORE_REDIS_URL": "redis://redis.example.test:6380/2"},
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert (
        production_environment["DIALECTICORE_REDIS_URL"]
        == "redis://redis.example.test:6380/2"
    )


def test_production_secrets_redis_url_is_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        env={"DIALECTICORE_REDIS_URL": "rediss://redis.prod.internal:6380/0"},
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert (
        production_environment["DIALECTICORE_REDIS_URL"]
        == "rediss://redis.prod.internal:6380/0"
    )


def test_compose_api_key_auth_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_AUTH_API_KEY_REFERENCE": "file:/run/private/api-key",
            "DIALECTICORE_AUTH_API_KEY_HEADER": "x-api-key",
            "DIALECTICORE_AUTH_ROLE_HEADER": "x-role",
            "DIALECTICORE_AUTH_USER_HEADER": "x-user",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert (
        production_environment["DIALECTICORE_AUTH_API_KEY_REFERENCE"]
        == "file:/run/private/api-key"
    )
    assert production_environment["DIALECTICORE_AUTH_API_KEY_HEADER"] == "x-api-key"
    assert production_environment["DIALECTICORE_AUTH_ROLE_HEADER"] == "x-role"
    assert production_environment["DIALECTICORE_AUTH_USER_HEADER"] == "x-user"


def test_production_secrets_api_key_reference_is_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        env={"DIALECTICORE_AUTH_API_KEY_REFERENCE": "docker-secret:custom_api_key"},
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert (
        production_environment["DIALECTICORE_AUTH_API_KEY_REFERENCE"]
        == "docker-secret:custom_api_key"
    )


def test_compose_api_healthcheck_probe_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_HEALTHCHECK_URL": (
                "http://127.0.0.1:9000/internal/ready"
            ),
            "DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS": "2.5",
            "DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES": "healthy",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_HEALTHCHECK_URL"] == (
        "http://127.0.0.1:9000/internal/ready"
    )
    assert production_environment["DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS"] == "2.5"
    assert production_environment["DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES"] == (
        "healthy"
    )


def test_compose_model_provider_is_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={"DIALECTICORE_MODEL_PROVIDER": "openai"},
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_MODEL_PROVIDER"] == "openai"


def test_compose_research_provider_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        env={
            "DIALECTICORE_RESEARCH_RETRIEVAL_TIMEOUT_SECONDS": "12.5",
            "DIALECTICORE_RESEARCH_RETRIEVAL_MAX_BYTES": "2500000",
            "DIALECTICORE_RESEARCH_DISCOVERY_ENABLED": "true",
            "DIALECTICORE_RESEARCH_DISCOVERY_URL_TEMPLATE": (
                "https://search.example.test/?q={query}"
            ),
            "DIALECTICORE_RESEARCH_DISCOVERY_MAX_QUERIES": "7",
            "DIALECTICORE_RESEARCH_DISCOVERY_MAX_RESULTS_PER_QUERY": "9",
            "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_ENABLED": "true",
            "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_URL": (
                "https://extract.example.test/api/extract"
            ),
            "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_TIMEOUT_SECONDS": "22",
            "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_SOURCES": "11",
            "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_CLAIMS_PER_SOURCE": "13",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_RESEARCH_RETRIEVAL_TIMEOUT_SECONDS"] == (
        "12.5"
    )
    assert production_environment["DIALECTICORE_RESEARCH_RETRIEVAL_MAX_BYTES"] == "2500000"
    assert production_environment["DIALECTICORE_RESEARCH_DISCOVERY_ENABLED"] == "true"
    assert production_environment["DIALECTICORE_RESEARCH_DISCOVERY_URL_TEMPLATE"] == (
        "https://search.example.test/?q={query}"
    )
    assert production_environment["DIALECTICORE_RESEARCH_DISCOVERY_MAX_QUERIES"] == "7"
    assert (
        production_environment["DIALECTICORE_RESEARCH_DISCOVERY_MAX_RESULTS_PER_QUERY"]
        == "9"
    )
    assert (
        production_environment["DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_ENABLED"]
        == "true"
    )
    assert production_environment["DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_URL"] == (
        "https://extract.example.test/api/extract"
    )
    assert (
        production_environment["DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_TIMEOUT_SECONDS"]
        == "22"
    )
    assert (
        production_environment["DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_SOURCES"]
        == "11"
    )
    assert (
        production_environment[
            "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_CLAIMS_PER_SOURCE"
        ]
        == "13"
    )


def test_production_secrets_api_healthcheck_probe_settings_are_operator_tunable() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        profiles=("temporal-external",),
        env={
            "DIALECTICORE_HEALTHCHECK_URL": (
                "http://127.0.0.1:9000/internal/ready"
            ),
            "DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS": "2.5",
        },
    )
    production_environment = config["services"]["production-api"]["environment"]

    assert production_environment["DIALECTICORE_HEALTHCHECK_URL"] == (
        "http://127.0.0.1:9000/internal/ready"
    )
    assert production_environment["DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS"] == "2.5"


def test_all_profiles_production_compose_render_preserves_operator_boundaries() -> None:
    config = _compose_config(
        "docker-compose.yml",
        "docker-compose.production-secrets.yml",
        profiles=("ops-ui", "temporal-external"),
    )
    services = config["services"]

    assert "ports" not in services["production-api"]
    assert "ports" not in services["minio"]
    assert services["web-ui"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["temporal-ui"]["ports"][0] == {
        "mode": "ingress",
        "host_ip": "127.0.0.1",
        "target": 8080,
        "published": "8088",
        "protocol": "tcp",
    }
    assert services["temporal-worker"]["environment"]["DIALECTICORE_WORKER_ROLE"] == (
        "temporal-worker"
    )
    assert services["temporal-worker"]["depends_on"] == {
        "production-api": {"condition": "service_healthy", "required": True},
        "temporal": {"condition": "service_healthy", "required": True},
    }


def test_compose_volume_topology_matches_runtime_state_media_and_backup_roles() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    api = _service_block(compose_text, "production-api")

    assert "- object-storage:/data/object-storage" in api
    assert "- runtime-state:/data/runtime-state" in api
    assert "- backups:/data/backups" in api

    for role in WORKER_SERVICE_ROLES:
        block = _service_block(compose_text, role)
        assert f"DIALECTICORE_WORKER_ROLE: {role}" in block
        assert "- runtime-state:/data/runtime-state" in block
        assert "- backups:/data/backups" not in block
        if role in MEDIA_WRITING_WORKER_ROLES:
            assert "- object-storage:/data/object-storage" in block
        if role in RUNTIME_ONLY_WORKER_ROLES:
            assert "- object-storage:/data/object-storage" not in block

    for volume_name in [
        "postgres-data",
        "redis-data",
        "minio-data",
        "object-storage",
        "runtime-state",
        "backups",
    ]:
        assert f"  {volume_name}:" in compose_text


def test_compose_health_gated_startup_order_matches_self_hosted_contract() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    api = _service_block(compose_text, "production-api")
    web_ui = _service_block(compose_text, "web-ui")
    temporal_worker = _service_block(compose_text, "temporal-worker")
    temporal_ui = _service_block(compose_text, "temporal-ui")
    postgres = _service_block(compose_text, "postgres")
    redis = _service_block(compose_text, "redis")
    minio = _service_block(compose_text, "minio")
    temporal = _service_block(compose_text, "temporal")

    assert "x-api-ready: &api-ready" in compose_text
    assert "production-api:\n    condition: service_healthy" in compose_text

    assert "postgres:\n        condition: service_healthy" in api
    assert "redis:\n        condition: service_healthy" in api
    assert "minio:\n        condition: service_healthy" in api
    assert 'test: ["CMD", "python", "-m", "app.healthcheck"]' in api
    assert "interval: ${DIALECTICORE_API_HEALTHCHECK_INTERVAL:-10s}" in api
    assert "timeout: ${DIALECTICORE_API_HEALTHCHECK_TIMEOUT:-6s}" in api
    assert "retries: ${DIALECTICORE_API_HEALTHCHECK_RETRIES:-12}" in api
    assert (
        "start_period: ${DIALECTICORE_API_HEALTHCHECK_START_PERIOD:-20s}"
        in api
    )

    assert 'test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-dialecticore}"]' in postgres
    assert 'test: ["CMD", "redis-cli", "ping"]' in redis
    assert (
        'test: ["CMD-SHELL", "curl -fsS '
        'http://127.0.0.1:9000/minio/health/live >/dev/null || exit 1"]'
    ) in minio
    assert "interval: ${DIALECTICORE_MINIO_HEALTHCHECK_INTERVAL:-10s}" in minio
    assert "timeout: ${DIALECTICORE_MINIO_HEALTHCHECK_TIMEOUT:-5s}" in minio
    assert "retries: ${DIALECTICORE_MINIO_HEALTHCHECK_RETRIES:-12}" in minio
    assert (
        "start_period: ${DIALECTICORE_MINIO_HEALTHCHECK_START_PERIOD:-10s}"
        in minio
    )
    assert (
        'test: ["CMD", "temporal", "operator", "cluster", "health", '
        '"--address", "127.0.0.1:7233", "--command-timeout", "5s"]'
    ) in temporal
    assert "interval: ${DIALECTICORE_TEMPORAL_HEALTHCHECK_INTERVAL:-10s}" in temporal
    assert "timeout: ${DIALECTICORE_TEMPORAL_HEALTHCHECK_TIMEOUT:-6s}" in temporal
    assert "retries: ${DIALECTICORE_TEMPORAL_HEALTHCHECK_RETRIES:-12}" in temporal
    assert (
        "start_period: ${DIALECTICORE_TEMPORAL_HEALTHCHECK_START_PERIOD:-20s}"
        in temporal
    )
    assert "interval: ${DIALECTICORE_POSTGRES_HEALTHCHECK_INTERVAL:-10s}" in postgres
    assert "timeout: ${DIALECTICORE_POSTGRES_HEALTHCHECK_TIMEOUT:-5s}" in postgres
    assert "retries: ${DIALECTICORE_POSTGRES_HEALTHCHECK_RETRIES:-5}" in postgres
    assert "interval: ${DIALECTICORE_REDIS_HEALTHCHECK_INTERVAL:-10s}" in redis
    assert "timeout: ${DIALECTICORE_REDIS_HEALTHCHECK_TIMEOUT:-5s}" in redis
    assert "retries: ${DIALECTICORE_REDIS_HEALTHCHECK_RETRIES:-5}" in redis

    assert "<<: *api-ready" in web_ui
    assert (
        'test: ["CMD-SHELL", "wget -qO- '
        'http://127.0.0.1:8080 >/dev/null 2>&1 || exit 1"]'
    ) in web_ui
    assert "interval: ${DIALECTICORE_WEB_HEALTHCHECK_INTERVAL:-10s}" in web_ui
    assert "timeout: ${DIALECTICORE_WEB_HEALTHCHECK_TIMEOUT:-5s}" in web_ui
    assert "retries: ${DIALECTICORE_WEB_HEALTHCHECK_RETRIES:-6}" in web_ui
    assert (
        "start_period: ${DIALECTICORE_WEB_HEALTHCHECK_START_PERIOD:-10s}"
        in web_ui
    )

    for role in NORMAL_WORKER_ROLES:
        block = _service_block(compose_text, role)
        assert "<<: *api-ready" in block
        assert "healthcheck: *worker-healthcheck" in block

    assert "temporal:\n        condition: service_healthy" in temporal_worker
    assert "production-api:\n        condition: service_healthy" in temporal_worker
    assert "healthcheck: *worker-healthcheck" in temporal_worker
    assert "temporal:\n        condition: service_healthy" in temporal_ui
    assert "postgres:\n        condition: service_healthy" in temporal
    for setting in [
        "DIALECTICORE_POSTGRES_HEALTHCHECK_INTERVAL=10s",
        "DIALECTICORE_POSTGRES_HEALTHCHECK_TIMEOUT=5s",
        "DIALECTICORE_POSTGRES_HEALTHCHECK_RETRIES=5",
        "DIALECTICORE_REDIS_HEALTHCHECK_INTERVAL=10s",
        "DIALECTICORE_REDIS_HEALTHCHECK_TIMEOUT=5s",
        "DIALECTICORE_REDIS_HEALTHCHECK_RETRIES=5",
        "DIALECTICORE_MINIO_HEALTHCHECK_INTERVAL=10s",
        "DIALECTICORE_MINIO_HEALTHCHECK_TIMEOUT=5s",
        "DIALECTICORE_MINIO_HEALTHCHECK_RETRIES=12",
        "DIALECTICORE_MINIO_HEALTHCHECK_START_PERIOD=10s",
        "DIALECTICORE_TEMPORAL_HEALTHCHECK_INTERVAL=10s",
        "DIALECTICORE_TEMPORAL_HEALTHCHECK_TIMEOUT=6s",
        "DIALECTICORE_TEMPORAL_HEALTHCHECK_RETRIES=12",
        "DIALECTICORE_TEMPORAL_HEALTHCHECK_START_PERIOD=20s",
    ]:
        assert setting in env_example


def test_compose_network_topology_separates_edge_from_backend_services() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "networks:\n  edge:\n  backend:" in compose_text

    for service_name in EDGE_SERVICE_ROLES:
        block = _service_block(compose_text, service_name)
        assert "networks:" in block
        assert "- edge" in block

    for service_name in BACKEND_SERVICE_ROLES:
        block = _service_block(compose_text, service_name)
        assert "networks:" in block
        assert "- backend" in block

    for service_name in [*WORKER_SERVICE_ROLES, *DATA_PLANE_SERVICE_ROLES, "temporal"]:
        assert "- edge" not in _service_block(compose_text, service_name)

    for service_name in DATA_PLANE_SERVICE_ROLES:
        block = _service_block(compose_text, service_name)
        assert "ports:" not in block or service_name == "minio"


def test_compose_published_ports_default_to_loopback_bindings() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    expected_bindings = {
        "web-ui": "${DIALECTICORE_WEB_BIND_ADDRESS:-127.0.0.1}:5173:8080",
        "production-api": "${DIALECTICORE_API_BIND_ADDRESS:-127.0.0.1}:8000:8000",
        "temporal-ui": "${DIALECTICORE_TEMPORAL_UI_BIND_ADDRESS:-127.0.0.1}:8088:8080",
        "minio": "${DIALECTICORE_MINIO_BIND_ADDRESS:-127.0.0.1}:9000:9000",
    }
    for service_name, binding in expected_bindings.items():
        block = _service_block(compose_text, service_name)
        assert f'- "{binding}"' in block

    minio = _service_block(compose_text, "minio")
    assert ":9001:9001" not in minio

    for setting in [
        "DIALECTICORE_WEB_BIND_ADDRESS=127.0.0.1",
        "DIALECTICORE_API_BIND_ADDRESS=127.0.0.1",
        "DIALECTICORE_TEMPORAL_UI_BIND_ADDRESS=127.0.0.1",
        "DIALECTICORE_MINIO_BIND_ADDRESS=127.0.0.1",
    ]:
        assert setting in env_example

    for service_name in [
        *WORKER_SERVICE_ROLES,
        "temporal",
        "postgres",
        "redis",
    ]:
        assert "ports:" not in _service_block(compose_text, service_name)


def test_compose_admin_ui_surfaces_are_profile_gated() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    temporal_ui = _service_block(compose_text, "temporal-ui")
    temporal = _service_block(compose_text, "temporal")

    assert "profiles:\n      - ops-ui" in temporal_ui
    assert "profiles:" not in temporal
    assert ":9001:9001" not in _service_block(compose_text, "minio")


def test_compose_services_have_self_hosted_restart_policy() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web_ui = _service_block(compose_text, "web-ui")

    assert "x-python-runtime-hardening: &python-runtime-hardening" in compose_text
    assert "x-web-runtime-hardening: &web-runtime-hardening" in compose_text
    assert (
        "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        in compose_text
    )
    assert "restart: unless-stopped" in compose_text[
        compose_text.index("x-python-runtime-hardening: &python-runtime-hardening") :
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening")
    ]
    assert "restart: unless-stopped" in compose_text[
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening") :
        compose_text.index("services:")
    ]
    infrastructure_hardening = compose_text[
        compose_text.index(
            "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        ) : compose_text.index("services:")
    ]
    assert "restart: unless-stopped" in infrastructure_hardening

    assert "<<: *web-runtime-hardening" in web_ui
    for service_name in ["production-api", *WORKER_SERVICE_ROLES]:
        assert "<<: *python-runtime-hardening" in _service_block(compose_text, service_name)

    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        assert INFRASTRUCTURE_RUNTIME_HARDENING in _service_block(
            compose_text, service_name
        )


def test_compose_services_use_bounded_json_logging() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "x-bounded-json-logging: &bounded-json-logging" in compose_text
    assert "driver: json-file" in compose_text
    assert "max-size: ${DIALECTICORE_DOCKER_LOG_MAX_SIZE:-10m}" in compose_text
    assert 'max-file: "${DIALECTICORE_DOCKER_LOG_MAX_FILE:-5}"' in compose_text
    assert "DIALECTICORE_DOCKER_LOG_MAX_SIZE=10m" in env_example
    assert "DIALECTICORE_DOCKER_LOG_MAX_FILE=5" in env_example

    python_hardening = compose_text[
        compose_text.index("x-python-runtime-hardening: &python-runtime-hardening") :
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening")
    ]
    web_hardening = compose_text[
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening") :
        compose_text.index("services:")
    ]
    assert RUNTIME_HARDENING_MERGE in python_hardening
    assert RUNTIME_HARDENING_MERGE in web_hardening

    for service_name in ["production-api", *WORKER_SERVICE_ROLES]:
        assert "<<: *python-runtime-hardening" in _service_block(compose_text, service_name)
    assert "<<: *web-runtime-hardening" in _service_block(compose_text, "web-ui")

    infrastructure_hardening = compose_text[
        compose_text.index(
            "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        ) : compose_text.index("services:")
    ]
    assert RUNTIME_HARDENING_MERGE in infrastructure_hardening
    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        assert INFRASTRUCTURE_RUNTIME_HARDENING in _service_block(
            compose_text, service_name
        )


def test_compose_services_have_init_and_graceful_stop_window() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DIALECTICORE_DOCKER_STOP_GRACE_PERIOD=60s" in env_example

    python_hardening = compose_text[
        compose_text.index("x-python-runtime-hardening: &python-runtime-hardening") :
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening")
    ]
    web_hardening = compose_text[
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening") :
        compose_text.index("services:")
    ]
    for hardening_block in [python_hardening, web_hardening]:
        assert "init: true" in hardening_block
        assert (
            "stop_grace_period: ${DIALECTICORE_DOCKER_STOP_GRACE_PERIOD:-60s}"
            in hardening_block
        )
    infrastructure_hardening = compose_text[
        compose_text.index(
            "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        ) : compose_text.index("services:")
    ]
    assert "init: true" in infrastructure_hardening
    assert (
        "stop_grace_period: ${DIALECTICORE_DOCKER_STOP_GRACE_PERIOD:-60s}"
        in infrastructure_hardening
    )

    for service_name in ["production-api", *WORKER_SERVICE_ROLES]:
        assert "<<: *python-runtime-hardening" in _service_block(compose_text, service_name)
    assert "<<: *web-runtime-hardening" in _service_block(compose_text, "web-ui")

    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        block = _service_block(compose_text, service_name)
        assert INFRASTRUCTURE_RUNTIME_HARDENING in block


def test_compose_infrastructure_services_disable_privilege_escalation() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    infrastructure_hardening = compose_text[
        compose_text.index(
            "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        ) : compose_text.index("services:")
    ]
    assert "security_opt:\n    - no-new-privileges:true" in infrastructure_hardening
    assert "read_only: true" not in infrastructure_hardening
    assert "cap_drop:" not in infrastructure_hardening

    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        block = _service_block(compose_text, service_name)
        assert INFRASTRUCTURE_RUNTIME_HARDENING in block


def test_compose_infrastructure_images_use_explicit_pull_policy() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    infrastructure_hardening = compose_text[
        compose_text.index(
            "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        ) : compose_text.index("services:")
    ]
    assert "pull_policy: missing" in infrastructure_hardening
    for setting in [
        "DIALECTICORE_POSTGRES_IMAGE=postgres:16",
        "DIALECTICORE_REDIS_IMAGE=redis:7-alpine",
        "DIALECTICORE_MINIO_IMAGE=minio/minio:RELEASE.2025-07-23T15-54-02Z",
        "DIALECTICORE_TEMPORAL_IMAGE=temporalio/auto-setup:1.25",
        "DIALECTICORE_TEMPORAL_UI_IMAGE=temporalio/ui:2.31.2",
    ]:
        assert setting in env_example
    for image_reference in [
        "image: ${DIALECTICORE_POSTGRES_IMAGE:-postgres:16}",
        "image: ${DIALECTICORE_REDIS_IMAGE:-redis:7-alpine}",
        (
            "image: ${DIALECTICORE_MINIO_IMAGE:-"
            "minio/minio:RELEASE.2025-07-23T15-54-02Z}"
        ),
        "image: ${DIALECTICORE_TEMPORAL_IMAGE:-temporalio/auto-setup:1.25}",
        "image: ${DIALECTICORE_TEMPORAL_UI_IMAGE:-temporalio/ui:2.31.2}",
    ]:
        assert image_reference in compose_text
    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        assert INFRASTRUCTURE_RUNTIME_HARDENING in _service_block(
            compose_text, service_name
        )


def test_compose_services_have_process_resource_limits() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "x-process-resource-limits: &process-resource-limits" in compose_text
    assert "cpus: ${DIALECTICORE_DOCKER_CPU_LIMIT:-2.0}" in compose_text
    assert "mem_limit: ${DIALECTICORE_DOCKER_MEMORY_LIMIT:-2g}" in compose_text
    assert "memswap_limit: ${DIALECTICORE_DOCKER_MEMORY_SWAP_LIMIT:-2g}" in compose_text
    assert "pids_limit: ${DIALECTICORE_DOCKER_PIDS_LIMIT:-512}" in compose_text
    assert "soft: ${DIALECTICORE_DOCKER_NOFILE_SOFT_LIMIT:-65536}" in compose_text
    assert "hard: ${DIALECTICORE_DOCKER_NOFILE_HARD_LIMIT:-65536}" in compose_text
    assert "DIALECTICORE_DOCKER_CPU_LIMIT=2.0" in env_example
    assert "DIALECTICORE_DOCKER_MEMORY_LIMIT=2g" in env_example
    assert "DIALECTICORE_DOCKER_MEMORY_SWAP_LIMIT=2g" in env_example
    assert "DIALECTICORE_DOCKER_PIDS_LIMIT=512" in env_example
    assert "DIALECTICORE_DOCKER_NOFILE_SOFT_LIMIT=65536" in env_example
    assert "DIALECTICORE_DOCKER_NOFILE_HARD_LIMIT=65536" in env_example
    assert "DIALECTICORE_DOCKER_PYTHON_TMPFS_SIZE=512m" in env_example
    assert "DIALECTICORE_DOCKER_WEB_TMPFS_SIZE=64m" in env_example

    python_hardening = compose_text[
        compose_text.index("x-python-runtime-hardening: &python-runtime-hardening") :
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening")
    ]
    web_hardening = compose_text[
        compose_text.index("x-web-runtime-hardening: &web-runtime-hardening") :
        compose_text.index("services:")
    ]
    assert RUNTIME_HARDENING_MERGE in python_hardening
    assert RUNTIME_HARDENING_MERGE in web_hardening
    assert (
        "- /tmp:rw,nosuid,nodev,size="
        "${DIALECTICORE_DOCKER_PYTHON_TMPFS_SIZE:-512m}"
    ) in python_hardening
    assert (
        "- /tmp:rw,nosuid,nodev,size=${DIALECTICORE_DOCKER_WEB_TMPFS_SIZE:-64m}"
    ) in web_hardening
    infrastructure_hardening = compose_text[
        compose_text.index(
            "x-infrastructure-runtime-hardening: &infrastructure-runtime-hardening"
        ) : compose_text.index("services:")
    ]
    assert RUNTIME_HARDENING_MERGE in infrastructure_hardening

    for service_name in ["production-api", *WORKER_SERVICE_ROLES]:
        assert "<<: *python-runtime-hardening" in _service_block(compose_text, service_name)
    assert "<<: *web-runtime-hardening" in _service_block(compose_text, "web-ui")

    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        assert INFRASTRUCTURE_RUNTIME_HARDENING in _service_block(
            compose_text, service_name
        )


def test_rendered_compose_services_preserve_runtime_hardening_defaults() -> None:
    config = _compose_config(
        "docker-compose.yml",
        profiles=("ops-ui", "temporal-external"),
    )
    services = config["services"]
    app_services = ["production-api", *WORKER_SERVICE_ROLES]

    for service_name in ["web-ui", *app_services, *INFRASTRUCTURE_SERVICE_ROLES]:
        service = services[service_name]
        assert service["init"] is True
        assert service["restart"] == "unless-stopped"
        assert service["stop_grace_period"] == "1m0s"
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-file": "5", "max-size": "10m"},
        }
        assert service["cpus"] == 2
        assert int(service["mem_limit"]) == 2 * 1024 * 1024 * 1024
        assert int(service["memswap_limit"]) == 2 * 1024 * 1024 * 1024
        assert service["pids_limit"] == 512
        assert service["ulimits"]["nofile"] == {"soft": 65536, "hard": 65536}

    for service_name in app_services:
        service = services[service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=512m"]

    web_ui = services["web-ui"]
    assert web_ui["read_only"] is True
    assert web_ui["cap_drop"] == ["ALL"]
    assert web_ui["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=64m"]

    for service_name in INFRASTRUCTURE_SERVICE_ROLES:
        service = services[service_name]
        assert service["pull_policy"] == "missing"
        assert "read_only" not in service
        assert "cap_drop" not in service
        assert "tmpfs" not in service


def test_compose_cors_origin_defaults_are_environment_driven() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    production_override = (ROOT / "docker-compose.production-secrets.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "DIALECTICORE_CORS_ALLOWED_ORIGINS: "
        "${DIALECTICORE_CORS_ALLOWED_ORIGINS:-*}"
    ) in compose_text
    assert (
        "DIALECTICORE_CORS_ALLOWED_ORIGINS: "
        "${DIALECTICORE_CORS_ALLOWED_ORIGINS:-http://localhost:5173}"
    ) in production_override


def test_compose_exposes_operator_hardening_and_research_settings() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for setting in [
        "DIALECTICORE_RUNTIME_PATH_MIN_FREE_BYTES",
        "DIALECTICORE_TEMPORAL_SIGNAL_TIMEOUT_SECONDS",
        "DIALECTICORE_WORKFLOW_STAGE_RETRY_MAX_ATTEMPTS",
        "DIALECTICORE_WORKFLOW_STAGE_RETRY_BACKOFF_SECONDS",
        "DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED",
        "DIALECTICORE_WORDS_PER_SECOND",
        "DIALECTICORE_RESEARCH_RETRIEVAL_TIMEOUT_SECONDS",
        "DIALECTICORE_RESEARCH_RETRIEVAL_MAX_BYTES",
        "DIALECTICORE_RESEARCH_DISCOVERY_ENABLED",
        "DIALECTICORE_RESEARCH_DISCOVERY_URL_TEMPLATE",
        "DIALECTICORE_RESEARCH_DISCOVERY_MAX_QUERIES",
        "DIALECTICORE_RESEARCH_DISCOVERY_MAX_RESULTS_PER_QUERY",
        "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_ENABLED",
        "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_URL",
        "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_TIMEOUT_SECONDS",
        "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_SOURCES",
        "DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_CLAIMS_PER_SOURCE",
    ]:
        assert f"{setting}:" in compose_text


def test_compose_operator_environment_variables_are_documented() -> None:
    compose_text = "\n".join(
        [
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
            (ROOT / "docker-compose.production-secrets.yml").read_text(
                encoding="utf-8"
            ),
        ]
    )
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose_variables = {
        match.group(1)
        for match in re.finditer(r"\$\{(DIALECTICORE_[A-Z0-9_]+)(?::-?[^}]*)?\}", compose_text)
    }
    documented_variables = {
        match.group(1)
        for match in re.finditer(r"^(DIALECTICORE_[A-Z0-9_]+)=", env_example, re.M)
    }

    assert compose_variables
    assert compose_variables - documented_variables == set()


def test_production_secrets_override_uses_secret_references_for_runtime_credentials() -> None:
    production_override = (ROOT / "docker-compose.production-secrets.yml").read_text(
        encoding="utf-8"
    )
    app_services = ["production-api", *WORKER_SERVICE_ROLES]

    assert "DIALECTICORE_ENV: production" in production_override
    assert 'DIALECTICORE_DATABASE_URL: ""' in production_override
    assert (
        "DIALECTICORE_DATABASE_PASSWORD_REFERENCE: "
        "${DIALECTICORE_DATABASE_PASSWORD_REFERENCE:-docker-secret:postgres_password}"
        in production_override
    )
    assert (
        "DIALECTICORE_AUTH_API_KEY_REFERENCE: "
        "${DIALECTICORE_AUTH_API_KEY_REFERENCE:-docker-secret:dialecticore_api_key}"
        in production_override
    )
    assert (
        "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE: "
        "${DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE:-docker-secret:minio_root_user}"
    ) in production_override
    assert (
        "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE: "
        "${DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE:-docker-secret:minio_root_password}"
    ) in production_override
    assert (
        "DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES: "
        "${DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES:-healthy,degraded}"
        in production_override
    )
    assert 'DIALECTICORE_API_KEY: ""' in production_override
    assert 'MINIO_ROOT_USER: ""' in production_override
    assert 'MINIO_ROOT_PASSWORD: ""' in production_override

    for secret_name in [
        "dialecticore_api_key",
        "postgres_password",
        "minio_root_user",
        "minio_root_password",
    ]:
        assert f"  - source: {secret_name}\n    target: {secret_name}\n    mode: 0444" in (
            production_override
        )

    for service_name in app_services:
        block = _service_block(production_override, service_name)
        assert "<<: *app-secret-environment" in block
        assert "secrets: *app-secrets" in block

    production_api = _service_block(production_override, "production-api")
    assert "ports: !reset []" in production_api

    for secret_name in [
        "dialecticore_api_key",
        "postgres_password",
        "minio_root_user",
        "minio_root_password",
    ]:
        assert f"  {secret_name}:\n    file: ./secrets/{secret_name}" in production_override

    postgres = _service_block(production_override, "postgres")
    assert "POSTGRES_PASSWORD: null" in postgres
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in postgres
    assert "source: postgres_password" in postgres
    assert "target: postgres_password" in postgres
    assert "mode: 0444" in postgres

    temporal = _service_block(production_override, "temporal")
    assert 'export POSTGRES_PWD="$$(cat /run/secrets/postgres_password)"' in temporal
    assert "POSTGRES_PWD: null" in temporal
    assert "source: postgres_password" in temporal
    assert "target: postgres_password" in temporal
    assert "mode: 0444" in temporal

    minio = _service_block(production_override, "minio")
    assert "MINIO_ROOT_USER: null" in minio
    assert "MINIO_ROOT_USER_FILE: /run/secrets/minio_root_user" in minio
    assert "MINIO_ROOT_PASSWORD: null" in minio
    assert "MINIO_ROOT_PASSWORD_FILE: /run/secrets/minio_root_password" in minio
    assert "ports: !reset []" in minio
    assert "source: minio_root_user" in minio
    assert "target: minio_root_user" in minio
    assert "source: minio_root_password" in minio
    assert "target: minio_root_password" in minio
    assert "mode: 0444" in minio


def test_python_runtime_images_keep_non_root_ffmpeg_and_data_directory_contract() -> None:
    api_dockerfile = (ROOT / "Dockerfile.production-api").read_text(encoding="utf-8")
    worker_dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    for dockerfile in [api_dockerfile, worker_dockerfile]:
        assert dockerfile.startswith("FROM python:3.12-slim\n")
        assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile
        assert "ENV PYTHONUNBUFFERED=1" in dockerfile
        assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile
        assert "rm -rf /var/lib/apt/lists/*" in dockerfile
        assert "RUN pip install --no-cache-dir ." in dockerfile
        assert "groupadd --system --gid 10001 dialecticore" in dockerfile
        assert (
            "useradd --system --uid 10001 --gid dialecticore --home-dir /app "
            "--no-create-home dialecticore"
        ) in dockerfile
        assert "mkdir -p /data/object-storage /data/runtime-state /data/backups" in dockerfile
        assert "chown -R dialecticore:dialecticore /app /data" in dockerfile
        assert "USER dialecticore" in dockerfile

    assert "EXPOSE 8000" in api_dockerfile
    assert (
        'CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app '
        '--app-dir backend --host 0.0.0.0 --port 8000"]'
    ) in api_dockerfile
    assert 'CMD ["python", "-m", "app.workflows.worker_placeholder"]' in worker_dockerfile


def test_web_ui_image_serves_static_build_through_unprivileged_nginx_proxy() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    nginx_conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    web_ui = _service_block(compose_text, "web-ui")

    assert dockerfile.startswith("FROM node:22-alpine AS build\n")
    assert "COPY package*.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "ARG VITE_API_BASE_URL=" in dockerfile
    assert "ARG VITE_DIALECTICORE_ROLE=producer" in dockerfile
    assert "ARG VITE_DIALECTICORE_USER=web-ui" in dockerfile
    assert "ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}" in dockerfile
    assert "ENV VITE_DIALECTICORE_ROLE=${VITE_DIALECTICORE_ROLE}" in dockerfile
    assert "ENV VITE_DIALECTICORE_USER=${VITE_DIALECTICORE_USER}" in dockerfile
    assert "ARG VITE_DIALECTICORE_API_KEY" not in dockerfile
    assert "ENV VITE_DIALECTICORE_API_KEY" not in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "FROM nginxinc/nginx-unprivileged:1.27-alpine" in dockerfile
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" in dockerfile
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in dockerfile
    assert "EXPOSE 8080" in dockerfile

    assert 'VITE_API_BASE_URL: ${DIALECTICORE_WEB_API_BASE_URL:-}' in web_ui
    assert 'VITE_DIALECTICORE_ROLE: ${DIALECTICORE_WEB_ROLE:-producer}' in web_ui
    assert 'VITE_DIALECTICORE_USER: ${DIALECTICORE_WEB_USER:-web-ui}' in web_ui
    assert '- "${DIALECTICORE_WEB_BIND_ADDRESS:-127.0.0.1}:5173:8080"' in web_ui
    assert (
        'test: ["CMD-SHELL", "wget -qO- '
        'http://127.0.0.1:8080 >/dev/null 2>&1 || exit 1"]'
    ) in web_ui
    assert "interval: ${DIALECTICORE_WEB_HEALTHCHECK_INTERVAL:-10s}" in web_ui
    assert "timeout: ${DIALECTICORE_WEB_HEALTHCHECK_TIMEOUT:-5s}" in web_ui
    assert "retries: ${DIALECTICORE_WEB_HEALTHCHECK_RETRIES:-6}" in web_ui
    assert (
        "start_period: ${DIALECTICORE_WEB_HEALTHCHECK_START_PERIOD:-10s}"
        in web_ui
    )
    for setting in [
        "DIALECTICORE_WEB_HEALTHCHECK_INTERVAL=10s",
        "DIALECTICORE_WEB_HEALTHCHECK_TIMEOUT=5s",
        "DIALECTICORE_WEB_HEALTHCHECK_RETRIES=6",
        "DIALECTICORE_WEB_HEALTHCHECK_START_PERIOD=10s",
    ]:
        assert setting in env_example

    assert "listen 8080;" in nginx_conf
    assert "root /usr/share/nginx/html;" in nginx_conf
    assert "location /api/ {" in nginx_conf
    assert "proxy_pass http://production-api:8000/api/;" in nginx_conf
    assert "proxy_http_version 1.1;" in nginx_conf
    assert "proxy_set_header Host $host;" in nginx_conf
    assert "proxy_set_header X-Real-IP $remote_addr;" in nginx_conf
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in nginx_conf
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in nginx_conf
    assert "proxy_buffering off;" in nginx_conf
    assert "try_files $uri $uri/ /index.html;" in nginx_conf


def test_production_api_healthcheck_uses_auth_aware_helper() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    production_api = _service_block(compose_text, "production-api")

    assert 'test: ["CMD", "python", "-m", "app.healthcheck"]' in production_api
    assert (
        "DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES: "
        "${DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES:-healthy,degraded}"
        in compose_text
    )
    for setting in [
        "DIALECTICORE_API_HEALTHCHECK_INTERVAL=10s",
        "DIALECTICORE_API_HEALTHCHECK_TIMEOUT=6s",
        "DIALECTICORE_API_HEALTHCHECK_RETRIES=12",
        "DIALECTICORE_API_HEALTHCHECK_START_PERIOD=20s",
        "DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES=healthy,degraded",
    ]:
        assert setting in env_example


def test_worker_services_use_heartbeat_healthcheck() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'test: ["CMD", "python", "-m", "app.worker_healthcheck"]' in compose_text
    assert "interval: ${DIALECTICORE_WORKER_HEALTHCHECK_INTERVAL:-30s}" in compose_text
    assert "timeout: ${DIALECTICORE_WORKER_HEALTHCHECK_TIMEOUT:-6s}" in compose_text
    assert "retries: ${DIALECTICORE_WORKER_HEALTHCHECK_RETRIES:-3}" in compose_text
    assert (
        "start_period: ${DIALECTICORE_WORKER_HEALTHCHECK_START_PERIOD:-30s}"
        in compose_text
    )
    for setting in [
        "DIALECTICORE_WORKER_HEALTHCHECK_INTERVAL=30s",
        "DIALECTICORE_WORKER_HEALTHCHECK_TIMEOUT=6s",
        "DIALECTICORE_WORKER_HEALTHCHECK_RETRIES=3",
        "DIALECTICORE_WORKER_HEALTHCHECK_START_PERIOD=30s",
    ]:
        assert setting in env_example
    for role in WORKER_SERVICE_ROLES:
        block = _service_block(compose_text, role)
        assert f"DIALECTICORE_WORKER_ROLE: {role}" in block
        assert "healthcheck: *worker-healthcheck" in block


def test_web_ui_nginx_uses_tmpfs_paths_for_read_only_container() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    nginx_conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    web_ui = _service_block(compose_text, "web-ui")

    assert "<<: *web-runtime-hardening" in web_ui
    assert "x-web-runtime-hardening: &web-runtime-hardening" in compose_text
    assert "read_only: true" in compose_text
    assert (
        "- /tmp:rw,nosuid,nodev,size=${DIALECTICORE_DOCKER_WEB_TMPFS_SIZE:-64m}"
        in compose_text
    )
    assert "client_body_temp_path /tmp/client_body_temp;" in nginx_conf
    assert "proxy_temp_path /tmp/proxy_temp;" in nginx_conf
    assert "fastcgi_temp_path /tmp/fastcgi_temp;" in nginx_conf
    assert "uwsgi_temp_path /tmp/uwsgi_temp;" in nginx_conf
    assert "scgi_temp_path /tmp/scgi_temp;" in nginx_conf


def test_production_build_context_excludes_development_artifacts() -> None:
    root_ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    frontend_ignore = (ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8")

    for pattern in [
        ".env",
        ".env.*",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".coverage",
        "coverage.xml",
        "htmlcov",
        "backend/tests",
        "backend/dialecticore.egg-info",
        "*.db",
        "storage",
        "secrets",
        "openrouter-api.txt",
        ".npmrc",
        ".pypirc",
        "pip.conf",
        ".netrc",
        ".aws",
        ".azure",
        ".config/gcloud",
        ".kube",
        "*.pem",
        "*.key",
        "*.crt",
    ]:
        assert pattern in root_ignore

    for pattern in [
        ".env",
        ".env.*",
        "node_modules",
        "dist",
        "tsconfig.tsbuildinfo",
        ".vite",
        ".cache",
        "coverage",
        "coverage-final.json",
        "secrets",
        "openrouter-api.txt",
        ".npmrc",
        ".pypirc",
        "pip.conf",
        ".netrc",
        ".aws",
        ".azure",
        ".config/gcloud",
        ".kube",
        "*.pem",
        "*.key",
        "*.crt",
    ]:
        assert pattern in frontend_ignore


def test_version_control_ignore_matches_local_secret_and_artifact_boundary() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in [
        ".env",
        ".env.*",
        "!.env.example",
        ".venv/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        ".coverage",
        "coverage.xml",
        "htmlcov/",
        "dialecticore-dev.db",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "backend/*.egg-info/",
        "frontend/node_modules/",
        "frontend/dist/",
        "frontend/*.tsbuildinfo",
        "storage/",
        "secrets/",
        "openrouter-api.txt",
        ".npmrc",
        ".pypirc",
        "pip.conf",
        ".netrc",
        ".aws/",
        ".azure/",
        ".config/gcloud/",
        ".kube/",
        "*.pem",
        "*.key",
        "*.crt",
    ]:
        assert pattern in gitignore
