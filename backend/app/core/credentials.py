from __future__ import annotations

SUPPORTED_CREDENTIAL_REFERENCE_SCHEMES = {"env", "file", "docker-secret"}
INVALID_CREDENTIAL_REFERENCE = "[invalid]"
UNSUPPORTED_CREDENTIAL_REFERENCE_TARGET = "[unsupported]"


def normalize_credential_reference(reference: str | None) -> str | None:
    if reference is None:
        return None
    value = reference.strip()
    if not value:
        return None
    scheme, separator, target = value.partition(":")
    if not separator or not scheme.strip() or not target.strip():
        raise ValueError("credential_reference must use scheme:target syntax")
    if any(character.isspace() for character in scheme):
        raise ValueError("credential_reference scheme must not contain whitespace")
    return value


def credential_reference_scheme(reference: str) -> str:
    try:
        normalized = normalize_credential_reference(reference)
    except ValueError:
        return "invalid"
    if normalized is None:
        return "invalid"
    scheme = normalized.split(":", maxsplit=1)[0]
    if scheme not in SUPPORTED_CREDENTIAL_REFERENCE_SCHEMES:
        return "unsupported"
    return scheme


def public_credential_reference(reference: str) -> str:
    try:
        normalized = normalize_credential_reference(reference)
    except ValueError:
        return INVALID_CREDENTIAL_REFERENCE
    if normalized is None:
        return INVALID_CREDENTIAL_REFERENCE
    scheme, target = normalized.split(":", maxsplit=1)
    if scheme not in SUPPORTED_CREDENTIAL_REFERENCE_SCHEMES:
        return f"{scheme}:{UNSUPPORTED_CREDENTIAL_REFERENCE_TARGET}"
    return normalized


def public_credential_target(reference: str) -> str:
    try:
        normalized = normalize_credential_reference(reference)
    except ValueError:
        return ""
    if normalized is None:
        return ""
    scheme, target = normalized.split(":", maxsplit=1)
    return target if scheme in SUPPORTED_CREDENTIAL_REFERENCE_SCHEMES else ""
