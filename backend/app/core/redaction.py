from __future__ import annotations

import re

REDACTED_VALUE = "[redacted]"
SENSITIVE_KEY_NAMES = {
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "client_secret",
    "password",
    "api_key",
    "authorization",
    "credential",
    "source_image",
    "source_image_artifact_id",
}
SENSITIVE_KEY_COMPACT_NAMES = {
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "token",
    "secret",
    "clientsecret",
    "password",
    "apikey",
    "authorization",
    "credential",
    "sourceimage",
    "sourceimageartifactid",
}
SENSITIVE_KEY_SUFFIXES = tuple(f"_{name}" for name in SENSITIVE_KEY_NAMES)
SENSITIVE_KEY_COMPACT_SUFFIXES = tuple(
    name for name in SENSITIVE_KEY_COMPACT_NAMES if name != "token"
)


def safe_provider_response_payload(payload: object) -> object:
    if isinstance(payload, dict):
        return {
            key: (
                REDACTED_VALUE
                if is_sensitive_provider_response_key(str(key))
                else safe_provider_response_payload(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [safe_provider_response_payload(value) for value in payload]
    return payload


def is_sensitive_provider_response_key(key: str) -> bool:
    normalized = _normalized_key(key)
    compact = normalized.replace("_", "")
    return (
        normalized in SENSITIVE_KEY_NAMES
        or compact in SENSITIVE_KEY_COMPACT_NAMES
        or normalized.endswith(SENSITIVE_KEY_SUFFIXES)
        or compact.endswith(SENSITIVE_KEY_COMPACT_SUFFIXES)
    )


def _normalized_key(key: str) -> str:
    return re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        "_",
        key.strip().replace("-", "_"),
    ).lower()
