from __future__ import annotations

import stat

import pytest
from app.bootstrap_admin import (
    create_admin_api_key_secret,
    format_admin_bootstrap_result,
    main,
)


def test_bootstrap_admin_creates_redacted_docker_secret_file(tmp_path) -> None:
    result = create_admin_api_key_secret(tmp_path)

    assert result.secret_path == tmp_path / "dialecticore_api_key"
    assert result.docker_secret_reference == "docker-secret:dialecticore_api_key"
    assert result.secret is None
    assert result.role == "admin"
    assert result.user == "bootstrap-admin"
    assert result.secret_path.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(result.secret_path.stat().st_mode) == 0o600
    formatted = format_admin_bootstrap_result(result)
    assert "api_key=<redacted>" in formatted
    assert result.secret_path.read_text(encoding="utf-8").strip() not in formatted


def test_bootstrap_admin_can_show_secret_when_requested(tmp_path) -> None:
    result = create_admin_api_key_secret(tmp_path, show_secret=True)

    assert result.secret
    assert result.secret_path.read_text(encoding="utf-8").strip() == result.secret
    assert f"api_key={result.secret}" in format_admin_bootstrap_result(result)


def test_bootstrap_admin_refuses_to_overwrite_without_force(tmp_path) -> None:
    first = create_admin_api_key_secret(tmp_path, show_secret=True)

    with pytest.raises(FileExistsError, match="already exists"):
        create_admin_api_key_secret(tmp_path)

    assert first.secret_path.read_text(encoding="utf-8").strip() == first.secret


def test_bootstrap_admin_force_rotates_existing_secret(tmp_path) -> None:
    first = create_admin_api_key_secret(tmp_path, show_secret=True)
    rotated = create_admin_api_key_secret(tmp_path, force=True, show_secret=True)

    assert rotated.secret_path == first.secret_path
    assert rotated.secret != first.secret
    assert rotated.secret_path.read_text(encoding="utf-8").strip() == rotated.secret
    assert stat.S_IMODE(rotated.secret_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("secret_name", ["", "../api_key", "nested/api_key", ".", ".."])
def test_bootstrap_admin_rejects_unsafe_secret_names(tmp_path, secret_name) -> None:
    with pytest.raises(ValueError, match="single Docker secret filename"):
        create_admin_api_key_secret(tmp_path, secret_name=secret_name)


def test_bootstrap_admin_requires_strong_token_size(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least 32 random bytes"):
        create_admin_api_key_secret(tmp_path, token_bytes=16)


def test_bootstrap_admin_cli_returns_nonzero_for_existing_secret(tmp_path, capsys) -> None:
    create_admin_api_key_secret(tmp_path)

    exit_code = main(["--secrets-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "already exists" in captured.err
