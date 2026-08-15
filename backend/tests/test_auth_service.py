import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.core.config import Settings
from app.domain.schemas import ProviderSessionRevocationRequest
from app.services.auth_service import ROLE_PERMISSIONS, ROUTE_PERMISSIONS, AuthService
from starlette.datastructures import Headers


def test_route_permission_vocabulary_covers_roles_and_protected_routes() -> None:
    service = AuthService(Settings(auth_enabled=False))
    role_permissions = {
        permission
        for permissions in ROLE_PERMISSIONS.values()
        for permission in permissions
        if permission != "*"
    }
    route_examples = [
        ("GET", "/api/v1/episodes", "read"),
        ("POST", "/api/v1/system/workers/heartbeat", "worker_heartbeat"),
        ("POST", "/api/v1/system/workers/signals", "workflow_control"),
        ("POST", "/api/v1/system/backups", "backup_create"),
        ("POST", "/api/v1/system/backups/restore", "backup_restore"),
        ("POST", "/api/v1/system/auth/provider-session/revocations", "configuration_write"),
        ("POST", "/api/v1/episodes", "episode_write"),
        ("POST", "/api/v1/episodes/episode-1/produce", "production_run"),
        ("POST", "/api/v1/episodes/episode-1/workflow/actions", "workflow_control"),
        ("POST", "/api/v1/episodes/episode-1/approvals/approval-1/decision", "approval_decide"),
        (
            "POST",
            "/api/v1/episodes/episode-1/discussion/turns/turn-1/regenerate",
            "transcript_edit",
        ),
        ("POST", "/api/v1/episodes/episode-1/research/build", "research_write"),
        ("POST", "/api/v1/episodes/episode-1/publish", "publish"),
        ("POST", "/api/v1/episodes/episode-1/audio-assets/generate", "media_generate"),
        ("POST", "/api/v1/model-endpoints", "configuration_write"),
        ("POST", "/api/v1/discussion-prompt-templates", "configuration_write"),
    ]

    assert role_permissions <= ROUTE_PERMISSIONS
    assert {expected for _, _, expected in route_examples} <= ROUTE_PERMISSIONS
    for method, path, expected in route_examples:
        assert service.permission_for_request(method, path) == expected


def test_auth_service_allows_development_when_disabled() -> None:
    service = AuthService(Settings(auth_enabled=False))

    context = service.authorize_request("POST", "/api/v1/system/backups/restore", Headers({}))
    policy = service.policy()

    assert context.enabled is False
    assert context.role == "admin"
    assert context.permission == "backup_restore"
    assert "backup_restore" in policy["permissions"]
    assert "configuration_write" in policy["permissions"]


def test_auth_service_enforces_api_key_and_role_permissions(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        )
    )

    with pytest.raises(PermissionError, match="valid API key"):
        service.authorize_request("GET", "/api/v1/episodes", Headers({}))

    viewer = service.authorize_request(
        "GET",
        "/api/v1/episodes",
        Headers(
            {
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "viewer",
            }
        ),
    )
    assert viewer.permission == "read"

    with pytest.raises(PermissionError, match="episode_write"):
        service.authorize_request(
            "POST",
            "/api/v1/episodes",
            Headers(
                {
                    "x-dialecticore-api-key": "secret",
                    "x-dialecticore-role": "viewer",
                }
            ),
        )

    producer = service.authorize_request(
        "POST",
        "/api/v1/episodes",
        Headers(
            {
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "producer",
                "x-dialecticore-user": "producer-1",
            }
        ),
    )
    assert producer.authenticated is True
    assert producer.user_id == "producer-1"
    assert producer.permission == "episode_write"

    with pytest.raises(PermissionError, match="configuration_write"):
        service.authorize_request(
            "POST",
            "/api/v1/model-endpoints",
            Headers(
                {
                    "x-dialecticore-api-key": "secret",
                    "x-dialecticore-role": "producer",
                }
            ),
        )
    with pytest.raises(PermissionError, match="configuration_write"):
        service.authorize_request(
            "POST",
            "/api/v1/projects",
            Headers(
                {
                    "x-dialecticore-api-key": "secret",
                    "x-dialecticore-role": "producer",
                }
            ),
        )

    admin = service.authorize_request(
        "POST",
        "/api/v1/model-endpoints",
        Headers(
            {
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "admin",
            }
        ),
    )
    assert admin.permission == "configuration_write"


def test_auth_service_accepts_trusted_identity_headers_with_group_mapping() -> None:
    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_trusted_identity_enabled=True,
            auth_trusted_group_role_map=(
                "dialecticore-admins=admin,dialecticore-producers=producer,"
                "dialecticore-editors=editor"
            ),
        )
    )

    producer = service.authorize_request(
        "POST",
        "/api/v1/episodes",
        Headers(
            {
                "x-forwarded-user": "user-123",
                "x-forwarded-email": "producer@example.test",
                "x-forwarded-groups": "other,dialecticore-producers",
            }
        ),
    )

    assert producer.authenticated is True
    assert producer.auth_mode == "trusted_identity"
    assert producer.identity_source == "x-forwarded-user"
    assert producer.user_id == "user-123"
    assert producer.role == "producer"
    assert producer.groups == ("other", "dialecticore-producers")


def test_auth_service_trusted_identity_defaults_to_viewer_and_falls_back_to_api_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
            auth_trusted_identity_enabled=True,
            auth_trusted_group_role_map="dialecticore-producers=producer",
        )
    )

    viewer = service.authorize_request(
        "GET",
        "/api/v1/episodes",
        Headers({"x-forwarded-user": "viewer-1"}),
    )
    assert viewer.role == "viewer"
    assert viewer.auth_mode == "trusted_identity"

    with pytest.raises(PermissionError, match="episode_write"):
        service.authorize_request(
            "POST",
            "/api/v1/episodes",
            Headers({"x-forwarded-user": "viewer-1"}),
        )

    api_key_admin = service.authorize_request(
        "POST",
        "/api/v1/model-endpoints",
        Headers(
            {
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "admin",
            }
        ),
    )
    assert api_key_admin.auth_mode == "api_key"
    assert api_key_admin.role == "admin"


def test_auth_service_accepts_provider_managed_session_with_group_mapping(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "active": True,
                "sub": "provider-user-1",
                "groups": ["ops", "dialecticore-producers"],
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
        )

    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_provider_session_enabled=True,
            auth_provider_session_introspection_url="https://idp.example.test/introspect",
            auth_provider_session_client_id_reference="env:OIDC_CLIENT_ID",
            auth_provider_session_client_secret_reference="env:OIDC_CLIENT_SECRET",
            auth_provider_session_group_role_map="dialecticore-producers=producer",
            auth_provider_session_decision_log_path=str(tmp_path / "decisions.json"),
        ),
        transport=httpx.MockTransport(handler),
    )

    producer = service.authorize_request(
        "POST",
        "/api/v1/episodes",
        Headers({"authorization": "Bearer provider-token"}),
    )

    assert seen["url"] == "https://idp.example.test/introspect"
    assert "token=provider-token" in seen["body"]
    assert "client_id=client-id" in seen["body"]
    assert "client_secret=client-secret" in seen["body"]
    assert producer.authenticated is True
    assert producer.auth_mode == "provider_session"
    assert producer.identity_source == "authorization"
    assert producer.user_id == "provider-user-1"
    assert producer.role == "producer"
    assert producer.groups == ("ops", "dialecticore-producers")
    assert producer.session_expires_at is not None
    decisions = service.list_provider_session_decisions()
    assert len(decisions) == 1
    assert decisions[0]["status"] == "accepted"
    assert decisions[0]["subject"] == "provider-user-1"
    assert decisions[0]["role"] == "producer"
    assert decisions[0]["permission"] == "episode_write"
    assert decisions[0]["token_sha256"].startswith("sha256:")


def test_auth_service_rejects_inactive_and_expired_provider_sessions(tmp_path) -> None:
    expired = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())
    responses = [
        httpx.Response(200, json={"active": False}),
        httpx.Response(200, json={"active": True, "sub": "user-1", "exp": expired}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_provider_session_enabled=True,
            auth_provider_session_introspection_url="https://idp.example.test/introspect",
            auth_provider_session_decision_log_path=str(tmp_path / "decisions.json"),
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PermissionError, match="inactive"):
        service.authorize_request(
            "GET",
            "/api/v1/episodes",
            Headers({"authorization": "Bearer inactive-token"}),
        )

    with pytest.raises(PermissionError, match="expired"):
        service.authorize_request(
            "GET",
            "/api/v1/episodes",
            Headers({"authorization": "Bearer expired-token"}),
        )
    decisions = service.list_provider_session_decisions()
    assert [decision["status"] for decision in decisions] == ["denied", "denied"]
    assert "expired" in decisions[0]["reason"]
    assert "inactive" in decisions[1]["reason"]


def test_auth_service_rejects_revoked_provider_sessions_by_token_hash_and_jti(
    tmp_path,
) -> None:
    responses = [
        {"active": True, "sub": "user-1", "jti": "session-1"},
        {"active": True, "sub": "user-2", "jti": "session-2"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_provider_session_enabled=True,
            auth_provider_session_introspection_url="https://idp.example.test/introspect",
            auth_provider_session_revocation_path=str(tmp_path / "revocations.json"),
            auth_provider_session_decision_log_path=str(tmp_path / "decisions.json"),
        ),
        transport=httpx.MockTransport(handler),
    )
    token_hash = "sha256:" + hashlib.sha256(b"revoked-token").hexdigest()
    by_hash = service.record_provider_session_revocation(
        ProviderSessionRevocationRequest(
            token_sha256=token_hash,
            reason="logout",
            user_id="operator",
        )
    )
    by_jti = service.record_provider_session_revocation(
        ProviderSessionRevocationRequest(jti="session-2", reason="idp webhook")
    )

    assert by_hash["token_sha256"] == token_hash
    assert by_hash["created_by"] == "operator"
    assert by_jti["jti"] == "session-2"

    with pytest.raises(PermissionError, match="revoked"):
        service.authorize_request(
            "GET",
            "/api/v1/episodes",
            Headers({"authorization": "Bearer revoked-token"}),
        )

    with pytest.raises(PermissionError, match="revoked"):
        service.authorize_request(
            "GET",
            "/api/v1/episodes",
            Headers({"authorization": "Bearer other-token"}),
        )
    decisions = service.list_provider_session_decisions()
    assert [decision["status"] for decision in decisions] == ["denied", "denied"]
    assert all("revoked" in decision["reason"] for decision in decisions)


def test_auth_service_limits_provider_session_decision_log(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"active": True, "sub": "user-1"})

    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_provider_session_enabled=True,
            auth_provider_session_introspection_url="https://idp.example.test/introspect",
            auth_provider_session_decision_log_path=str(tmp_path / "decisions.json"),
            auth_provider_session_decision_log_limit=2,
        ),
        transport=httpx.MockTransport(handler),
    )

    for index in range(3):
        service.authorize_request(
            "GET",
            "/api/v1/episodes",
            Headers({"authorization": f"Bearer provider-token-{index}"}),
        )

    decisions = service.list_provider_session_decisions()
    assert len(decisions) == 2
    assert decisions[0]["token_sha256"] == "sha256:" + hashlib.sha256(
        b"provider-token-2"
    ).hexdigest()
    assert decisions[1]["token_sha256"] == "sha256:" + hashlib.sha256(
        b"provider-token-1"
    ).hexdigest()


def test_auth_service_prunes_expired_provider_session_revocations(tmp_path) -> None:
    service = AuthService(
        Settings(
            auth_enabled=True,
            auth_provider_session_enabled=True,
            auth_provider_session_introspection_url="https://idp.example.test/introspect",
            auth_provider_session_revocation_path=str(tmp_path / "revocations.json"),
        )
    )
    service.record_provider_session_revocation(
        ProviderSessionRevocationRequest(
            subject="old-user",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    active = service.record_provider_session_revocation(
        ProviderSessionRevocationRequest(subject="active-user")
    )

    assert service.list_provider_session_revocations() == [active]
