from __future__ import annotations

import app.healthcheck as healthcheck_module
import pytest
from app.core.config import Settings
from app.healthcheck import (
    _allowed_statuses_from_env,
    _timeout_seconds_from_env,
    healthcheck_headers,
    probe_health,
)
from app.services.model_gateway import SecretResolver


class FakeHealthResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHealthResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_healthcheck_uses_no_auth_headers_when_auth_disabled() -> None:
    headers = healthcheck_headers(Settings(auth_enabled=False))

    assert headers == {}


def test_healthcheck_adds_api_key_headers_when_auth_enabled(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "health-secret")
    headers = healthcheck_headers(
        Settings(
            auth_enabled=True,
            auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        )
    )

    assert headers == {
        "x-dialecticore-api-key": "health-secret",
        "x-dialecticore-role": "admin",
        "x-dialecticore-user": "container-healthcheck",
    }


def test_healthcheck_rejects_blank_api_key_header(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "health-secret")

    with pytest.raises(RuntimeError, match="DIALECTICORE_AUTH_API_KEY_HEADER"):
        healthcheck_headers(
            Settings(
                auth_enabled=True,
                auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
                auth_api_key_header=" ",
            )
        )


def test_healthcheck_rejects_blank_role_or_user_header(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "health-secret")

    with pytest.raises(RuntimeError, match="DIALECTICORE_AUTH_ROLE_HEADER"):
        healthcheck_headers(
            Settings(
                auth_enabled=True,
                auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
                auth_role_header="",
            )
        )

    with pytest.raises(RuntimeError, match="DIALECTICORE_AUTH_USER_HEADER"):
        healthcheck_headers(
            Settings(
                auth_enabled=True,
                auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
                auth_user_header=" ",
            )
        )


def test_healthcheck_uses_trusted_identity_headers_when_api_key_is_not_configured() -> None:
    headers = healthcheck_headers(
        Settings(
            auth_enabled=True,
            auth_api_key_reference=None,
            auth_trusted_identity_enabled=True,
        )
    )

    assert headers == {
        "x-forwarded-user": "container-healthcheck",
        "x-forwarded-email": "container-healthcheck@localhost",
    }


def test_healthcheck_rejects_blank_trusted_identity_headers() -> None:
    with pytest.raises(RuntimeError, match="DIALECTICORE_AUTH_TRUSTED_IDENTITY_HEADER"):
        healthcheck_headers(
            Settings(
                auth_enabled=True,
                auth_api_key_reference=None,
                auth_trusted_identity_enabled=True,
                auth_trusted_identity_header="",
            )
        )

    with pytest.raises(RuntimeError, match="DIALECTICORE_AUTH_TRUSTED_EMAIL_HEADER"):
        healthcheck_headers(
            Settings(
                auth_enabled=True,
                auth_api_key_reference=None,
                auth_trusted_identity_enabled=True,
                auth_trusted_email_header=" ",
            )
        )


def test_healthcheck_reports_missing_viable_auth_mode() -> None:
    with pytest.raises(RuntimeError, match="authenticated healthchecks require"):
        healthcheck_headers(Settings(auth_enabled=True, auth_api_key_reference=None))


def test_healthcheck_can_resolve_docker_secret_reference(tmp_path) -> None:
    secret_path = tmp_path / "dialecticore_api_key"
    secret_path.write_text("docker-health-secret\n", encoding="utf-8")
    resolver = SecretResolver()
    resolver.docker_secret_root = tmp_path

    headers = healthcheck_headers(
        Settings(
            auth_enabled=True,
            auth_api_key_reference="docker-secret:dialecticore_api_key",
        ),
        secret_resolver=resolver,
    )

    assert headers["x-dialecticore-api-key"] == "docker-health-secret"


def test_healthcheck_does_not_echo_raw_invalid_api_key_reference() -> None:
    raw_token = "leaked-raw-healthcheck-api-key"

    with pytest.raises(RuntimeError) as exc:
        healthcheck_headers(
            Settings(
                auth_enabled=True,
                auth_api_key_reference=raw_token,
            )
        )

    assert "credential_reference must use scheme:target syntax" in str(exc.value)
    assert raw_token not in str(exc.value)


def test_probe_health_accepts_degraded_status(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        assert request.full_url == "http://api/health"
        assert timeout == 2.0
        return FakeHealthResponse(b'{"status": "degraded"}')

    monkeypatch.setattr(healthcheck_module.urllib.request, "urlopen", fake_urlopen)

    body = probe_health(
        "http://api/health",
        Settings(auth_enabled=False),
        timeout_seconds=2.0,
    )

    assert body == b'{"status": "degraded"}'


def test_probe_health_rejects_unhealthy_status(monkeypatch) -> None:
    monkeypatch.setattr(
        healthcheck_module.urllib.request,
        "urlopen",
        lambda request, timeout: FakeHealthResponse(b'{"status": "unhealthy"}'),
    )

    with pytest.raises(RuntimeError, match="healthcheck status unhealthy"):
        probe_health("http://api/health", Settings(auth_enabled=False))


def test_probe_health_rejects_malformed_json(monkeypatch) -> None:
    monkeypatch.setattr(
        healthcheck_module.urllib.request,
        "urlopen",
        lambda request, timeout: FakeHealthResponse(b"<html>ok</html>"),
    )

    with pytest.raises(RuntimeError, match="not valid JSON"):
        probe_health("http://api/health", Settings(auth_enabled=False))


def test_healthcheck_allowed_statuses_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES", "healthy")

    assert _allowed_statuses_from_env() == {"healthy"}


def test_healthcheck_timeout_seconds_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS", "2.5")

    assert _timeout_seconds_from_env() == 2.5


def test_healthcheck_timeout_seconds_rejects_invalid_values(monkeypatch) -> None:
    for value in ["", "not-a-number", "0", "-1"]:
        monkeypatch.setenv("DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS", value)
        with pytest.raises(RuntimeError, match="must be a positive number"):
            _timeout_seconds_from_env()


def test_healthcheck_main_uses_probe_url_timeout_and_allowed_statuses(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_probe_health(
        url,
        settings,
        timeout_seconds=5.0,
        secret_resolver=None,
        allowed_statuses=None,
    ):
        observed["url"] = url
        observed["timeout_seconds"] = timeout_seconds
        observed["allowed_statuses"] = allowed_statuses
        return b'{"status": "healthy"}'

    monkeypatch.setenv(
        "DIALECTICORE_HEALTHCHECK_URL",
        "http://127.0.0.1:9000/internal/ready",
    )
    monkeypatch.setenv("DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES", "healthy")
    monkeypatch.setattr(healthcheck_module, "probe_health", fake_probe_health)

    assert healthcheck_module.main() == 0
    assert observed == {
        "url": "http://127.0.0.1:9000/internal/ready",
        "timeout_seconds": 2.5,
        "allowed_statuses": {"healthy"},
    }


def test_healthcheck_main_reports_invalid_timeout_without_traceback(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS", "not-a-number")

    assert healthcheck_module.main() == 1
    captured = capsys.readouterr()
    assert (
        "healthcheck failed: DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS "
        "must be a positive number"
    ) in captured.err
