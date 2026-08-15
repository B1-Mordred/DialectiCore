from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from app.core.config import Settings
from app.services.model_gateway import SecretResolver


def healthcheck_headers(
    settings: Settings,
    secret_resolver: SecretResolver | None = None,
) -> dict[str, str]:
    if not settings.auth_enabled:
        return {}
    if settings.auth_api_key_reference:
        return _api_key_healthcheck_headers(settings, secret_resolver)
    if settings.auth_trusted_identity_enabled:
        return _trusted_identity_healthcheck_headers(settings)
    raise RuntimeError(
        "authenticated healthchecks require DIALECTICORE_AUTH_API_KEY_REFERENCE "
        "or DIALECTICORE_AUTH_TRUSTED_IDENTITY_ENABLED"
    )


def _api_key_healthcheck_headers(
    settings: Settings,
    secret_resolver: SecretResolver | None = None,
) -> dict[str, str]:
    resolver = secret_resolver or SecretResolver()
    token = resolver.resolve(settings.auth_api_key_reference)
    if not token:
        raise RuntimeError("configured healthcheck API key reference did not resolve a value")
    api_key_header = _required_header_name(
        settings.auth_api_key_header,
        "DIALECTICORE_AUTH_API_KEY_HEADER",
    )
    role_header = _required_header_name(
        settings.auth_role_header,
        "DIALECTICORE_AUTH_ROLE_HEADER",
    )
    user_header = _required_header_name(
        settings.auth_user_header,
        "DIALECTICORE_AUTH_USER_HEADER",
    )
    headers = {
        api_key_header: token,
        role_header: "admin",
        user_header: "container-healthcheck",
    }
    return {name: value for name, value in headers.items() if value}


def _trusted_identity_healthcheck_headers(settings: Settings) -> dict[str, str]:
    identity_header = _required_header_name(
        settings.auth_trusted_identity_header,
        "DIALECTICORE_AUTH_TRUSTED_IDENTITY_HEADER",
    )
    email_header = _required_header_name(
        settings.auth_trusted_email_header,
        "DIALECTICORE_AUTH_TRUSTED_EMAIL_HEADER",
    )
    headers = {
        identity_header: "container-healthcheck",
        email_header: "container-healthcheck@localhost",
    }
    return {name: value for name, value in headers.items() if value}


def _required_header_name(value: str, setting_name: str) -> str:
    header = value.strip()
    if not header:
        raise RuntimeError(f"{setting_name} must be configured for authenticated healthchecks")
    return header


def probe_health(
    url: str,
    settings: Settings,
    timeout_seconds: float = 5.0,
    secret_resolver: SecretResolver | None = None,
    allowed_statuses: set[str] | None = None,
) -> bytes:
    request = urllib.request.Request(
        url,
        headers=healthcheck_headers(settings, secret_resolver),
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read()
    _validate_health_payload(body, allowed_statuses or {"healthy", "degraded"})
    return body


def _validate_health_payload(body: bytes, allowed_statuses: set[str]) -> None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("healthcheck response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("healthcheck response was not a JSON object")
    status = str(payload.get("status") or "").strip().lower()
    if status not in allowed_statuses:
        allowed = ", ".join(sorted(allowed_statuses))
        raise RuntimeError(
            f"healthcheck status {status or 'missing'} is not one of: {allowed}"
        )


def _allowed_statuses_from_env() -> set[str]:
    raw = os.getenv("DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES", "healthy,degraded")
    statuses = {status.strip().lower() for status in raw.split(",") if status.strip()}
    return statuses or {"healthy", "degraded"}


def _timeout_seconds_from_env() -> float:
    raw = os.getenv("DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS", "5").strip()
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            "DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS must be a positive number"
        ) from exc
    if timeout <= 0:
        raise RuntimeError(
            "DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS must be a positive number"
        )
    return timeout


def main() -> int:
    settings = Settings()
    try:
        url = os.getenv(
            "DIALECTICORE_HEALTHCHECK_URL",
            f"http://127.0.0.1:{settings.api_port}/api/v1/system/health",
        )
        probe_health(
            url,
            settings,
            timeout_seconds=_timeout_seconds_from_env(),
            allowed_statuses=_allowed_statuses_from_env(),
        )
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
