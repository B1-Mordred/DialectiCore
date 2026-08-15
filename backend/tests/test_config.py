from pathlib import Path

import pytest
from app.core.config import Settings


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    ).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.split("=", 1)[0])
    return keys


def test_resolved_database_url_uses_configured_database_url() -> None:
    settings = Settings(database_url="sqlite:///./local.db")

    assert settings.resolved_database_url() == "sqlite:///./local.db"


def test_resolved_database_url_assembles_from_secret_reference(tmp_path: Path) -> None:
    secret_path = tmp_path / "postgres-password"
    secret_path.write_text("p@ss word\n", encoding="utf-8")
    settings = Settings(
        database_url="",
        database_driver="postgresql+psycopg",
        database_host="postgres",
        database_port=5432,
        database_name="dialecticore",
        database_user="dialecticore",
        database_password_reference=f"file:{secret_path}",
    )

    assert (
        settings.resolved_database_url()
        == "postgresql+psycopg://dialecticore:p%40ss%20word@postgres:5432/dialecticore"
    )


def test_resolved_database_url_does_not_echo_raw_invalid_reference() -> None:
    raw_token = "leaked-raw-database-password"
    settings = Settings(database_url="", database_password_reference=raw_token)

    with pytest.raises(RuntimeError) as exc:
        settings.resolved_database_url()

    assert "credential_reference must use scheme:target syntax" in str(exc.value)
    assert raw_token not in str(exc.value)


def test_resolved_cors_allowed_origins_splits_and_trims() -> None:
    settings = Settings(
        cors_allowed_origins=(
            " https://studio.example.test,https://ops.example.test , ,"
        )
    )

    assert settings.resolved_cors_allowed_origins() == [
        "https://studio.example.test",
        "https://ops.example.test",
    ]


def test_env_example_exposes_non_secret_settings_fields() -> None:
    raw_secret_fields = {"database_password"}
    expected = {
        f"DIALECTICORE_{field_name.upper()}"
        for field_name in Settings.model_fields
        if field_name not in raw_secret_fields
    }

    assert sorted(expected - _env_example_keys()) == []
