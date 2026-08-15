from app.services.redaction import (
    is_sensitive_provider_response_key,
    safe_provider_response_payload,
)


def test_sensitive_provider_response_key_variants_are_detected() -> None:
    for key in (
        "access_token",
        "access-token",
        "accessToken",
        "AccessToken",
        "client_secret",
        "clientSecret",
        "api_key",
        "apiKey",
        "nestedPassword",
        "oauthRefreshToken",
        "request_authorization",
        "providerCredential",
    ):
        assert is_sensitive_provider_response_key(key)

    assert not is_sensitive_provider_response_key("status")
    assert not is_sensitive_provider_response_key("token_usage")
    assert not is_sensitive_provider_response_key("completion_tokens")


def test_safe_provider_response_payload_recursively_redacts_sensitive_values() -> None:
    payload = {
        "status": "completed",
        "accessToken": "leaked-token",
        "usage": {"completion_tokens": 12},
        "nested": {
            "clientSecret": "leaked-secret",
            "items": [{"api-key": "leaked-api-key"}, {"label": "safe"}],
        },
    }

    redacted = safe_provider_response_payload(payload)

    assert redacted == {
        "status": "completed",
        "accessToken": "[redacted]",
        "usage": {"completion_tokens": 12},
        "nested": {
            "clientSecret": "[redacted]",
            "items": [{"api-key": "[redacted]"}, {"label": "safe"}],
        },
    }
