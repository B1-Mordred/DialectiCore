from pathlib import Path

import pytest
from app.services.model_gateway import SecretResolver


def test_secret_resolver_reads_env_file_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "openrouter-token"
    secret_path.write_text("openrouter-secret\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(secret_path))

    assert SecretResolver().resolve("env:OPENROUTER_API_KEY") == "openrouter-secret"


def test_secret_resolver_prefers_env_value_over_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "b1-token"
    secret_path.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("B1_API_KEY", "direct-env-token")
    monkeypatch.setenv("B1_API_KEY_FILE", str(secret_path))

    assert SecretResolver().resolve("env:B1_API_KEY") == "direct-env-token"


def test_secret_resolver_rejects_relative_env_file_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("B1_API_KEY", raising=False)
    monkeypatch.setenv("B1_API_KEY_FILE", "relative/token")

    with pytest.raises(
        RuntimeError,
        match="env credential file reference must use an absolute path",
    ):
        SecretResolver().resolve("env:B1_API_KEY")


def test_secret_resolver_reads_file_reference(tmp_path: Path) -> None:
    secret_path = tmp_path / "provider-token"
    secret_path.write_text("file-token\n", encoding="utf-8")

    assert SecretResolver().resolve(f"file:{secret_path}") == "file-token"


def test_secret_resolver_rejects_relative_file_reference() -> None:
    with pytest.raises(RuntimeError, match="must use an absolute path"):
        SecretResolver().resolve("file:relative/token")


def test_secret_resolver_reads_docker_secret_reference(tmp_path: Path) -> None:
    secret_path = tmp_path / "youtube-token"
    secret_path.write_text("docker-token\r\n", encoding="utf-8")
    resolver = SecretResolver()
    resolver.docker_secret_root = tmp_path

    assert resolver.resolve("docker-secret:youtube-token") == "docker-token"


def test_secret_resolver_rejects_docker_secret_path_traversal() -> None:
    with pytest.raises(RuntimeError, match="docker-secret credential reference is invalid"):
        SecretResolver().resolve("docker-secret:../token")


def test_secret_resolver_does_not_echo_raw_invalid_reference() -> None:
    raw_token = "leaked-raw-provider-token"

    with pytest.raises(RuntimeError) as exc:
        SecretResolver().resolve(raw_token)

    assert "credential_reference must use scheme:target syntax" in str(exc.value)
    assert raw_token not in str(exc.value)


def test_secret_resolver_reports_missing_secret_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not available"):
        SecretResolver().resolve(f"file:{tmp_path / 'missing'}")
