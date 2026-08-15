from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from app.core.config import Settings
from app.domain.schemas import ProviderSessionRevocationRequest
from app.services.model_gateway import SecretResolver
from starlette.datastructures import Headers

ROLE_PERMISSIONS = {
    "viewer": {"read"},
    "reviewer": {"read", "approval_decide"},
    "editor": {
        "read",
        "approval_decide",
        "episode_write",
        "research_write",
        "transcript_edit",
        "media_generate",
    },
    "producer": {
        "read",
        "approval_decide",
        "episode_write",
        "production_run",
        "workflow_control",
        "research_write",
        "transcript_edit",
        "media_generate",
        "backup_create",
        "publish",
        "worker_heartbeat",
    },
    "admin": {"*"},
}

ROUTE_PERMISSIONS = {
    "approval_decide",
    "backup_create",
    "backup_restore",
    "configuration_write",
    "episode_write",
    "media_generate",
    "production_run",
    "publish",
    "read",
    "research_write",
    "transcript_edit",
    "worker_heartbeat",
    "workflow_control",
}


@dataclass(frozen=True)
class AuthContext:
    enabled: bool
    authenticated: bool
    role: str
    user_id: str
    permission: str
    auth_mode: Literal["disabled", "api_key", "trusted_identity", "provider_session"] = "disabled"
    identity_source: str | None = None
    groups: tuple[str, ...] = ()
    session_expires_at: str | None = None


class AuthService:
    def __init__(
        self,
        settings: Settings,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.secret_resolver = secret_resolver or SecretResolver()
        self.transport = transport

    def authorize_request(self, method: str, path: str, headers: Headers) -> AuthContext:
        permission = self.permission_for_request(method, path)
        if not self.settings.auth_enabled:
            return AuthContext(
                enabled=False,
                authenticated=False,
                role="admin",
                user_id="development",
                permission=permission,
                auth_mode="disabled",
            )

        provider_context = self._provider_session_context(permission, method, path, headers)
        if provider_context is not None:
            return provider_context

        trusted_context = self._trusted_identity_context(permission, headers)
        if trusted_context is not None:
            return trusted_context

        expected_key = self._expected_api_key()
        provided_key = headers.get(self.settings.auth_api_key_header)
        if not provided_key or provided_key != expected_key:
            raise PermissionError("valid API key is required")

        role = (headers.get(self.settings.auth_role_header) or "viewer").strip().lower()
        if role not in ROLE_PERMISSIONS:
            raise PermissionError(f"unknown role: {role}")
        if not self._role_allows(role, permission):
            raise PermissionError(f"role {role} lacks permission {permission}")
        return AuthContext(
            enabled=True,
            authenticated=True,
            role=role,
            user_id=headers.get(self.settings.auth_user_header) or role,
            permission=permission,
            auth_mode="api_key",
            identity_source=self.settings.auth_api_key_header,
        )

    def policy(self) -> dict:
        group_role_map = self._trusted_group_role_map()
        provider_group_role_map = self._provider_session_group_role_map()
        return {
            "enabled": self.settings.auth_enabled,
            "authentication_modes": {
                "api_key": {
                    "enabled": self.settings.auth_enabled,
                    "reference_configured": self.settings.auth_api_key_reference is not None,
                    "header": self.settings.auth_api_key_header,
                },
                "trusted_identity": {
                    "enabled": self.settings.auth_trusted_identity_enabled,
                    "identity_header": self.settings.auth_trusted_identity_header,
                    "email_header": self.settings.auth_trusted_email_header,
                    "groups_header": self.settings.auth_trusted_groups_header,
                    "default_role": self.settings.auth_trusted_default_role,
                    "group_role_map_configured": bool(group_role_map),
                    "mapped_groups": sorted(group_role_map),
                },
                "provider_session": {
                    "enabled": self.settings.auth_provider_session_enabled,
                    "introspection_url_configured": (
                        self.settings.auth_provider_session_introspection_url is not None
                    ),
                    "client_id_reference_configured": (
                        self.settings.auth_provider_session_client_id_reference is not None
                    ),
                    "client_secret_reference_configured": (
                        self.settings.auth_provider_session_client_secret_reference is not None
                    ),
                    "token_header": self.settings.auth_provider_session_token_header,
                    "user_claim": self.settings.auth_provider_session_user_claim,
                    "groups_claim": self.settings.auth_provider_session_groups_claim,
                    "default_role": self.settings.auth_provider_session_default_role,
                    "group_role_map_configured": bool(provider_group_role_map),
                    "mapped_groups": sorted(provider_group_role_map),
                    "revocation_registry": {
                        "enabled": self.settings.auth_provider_session_enabled,
                        "active_count": len(self.list_provider_session_revocations()),
                    },
                    "decision_log": {
                        "enabled": self.settings.auth_provider_session_enabled,
                        "retained_count": len(self.list_provider_session_decisions()),
                        "retention_limit": self.settings.auth_provider_session_decision_log_limit,
                    },
                },
            },
            "roles": {
                role: sorted(permissions)
                for role, permissions in ROLE_PERMISSIONS.items()
            },
            "headers": {
                "api_key": self.settings.auth_api_key_header,
                "role": self.settings.auth_role_header,
                "user": self.settings.auth_user_header,
                "trusted_identity": self.settings.auth_trusted_identity_header,
                "trusted_email": self.settings.auth_trusted_email_header,
                "trusted_groups": self.settings.auth_trusted_groups_header,
                "provider_session_token": self.settings.auth_provider_session_token_header,
            },
            "api_key_reference_configured": self.settings.auth_api_key_reference is not None,
            "trusted_identity_enabled": self.settings.auth_trusted_identity_enabled,
            "trusted_identity_group_role_map_configured": bool(group_role_map),
            "provider_session_enabled": self.settings.auth_provider_session_enabled,
            "provider_session_group_role_map_configured": bool(provider_group_role_map),
            "permissions": sorted(
                {
                    permission
                    for permissions in ROLE_PERMISSIONS.values()
                    for permission in permissions
                    if permission != "*"
                }
                | ROUTE_PERMISSIONS
            ),
        }

    def permission_for_request(self, method: str, path: str) -> str:
        normalized_method = method.upper()
        if normalized_method in {"GET", "HEAD", "OPTIONS"}:
            return "read"
        route = path.removeprefix("/api/v1")

        if route == "/system/workers/heartbeat":
            return "worker_heartbeat"
        if route == "/system/workers/signals":
            return "workflow_control"
        if route == "/system/backups":
            return "backup_create"
        if route == "/system/backups/restore":
            return "backup_restore"
        if route.startswith("/system/auth/"):
            return "configuration_write"
        if route == "/episodes":
            return "episode_write"
        if route.endswith("/produce"):
            return "production_run"
        if "/workflow/actions" in route:
            return "workflow_control"
        if "/approvals/" in route:
            return "approval_decide"
        if "/discussion/turns/" in route:
            return "transcript_edit"
        if "/research/" in route:
            return "research_write"
        if route.endswith("/publish"):
            return "publish"
        if self._is_media_route(route):
            return "media_generate"
        if self._is_configuration_route(route):
            return "configuration_write"
        return "production_run"

    def _provider_session_context(
        self,
        permission: str,
        method: str,
        path: str,
        headers: Headers,
    ) -> AuthContext | None:
        if not self.settings.auth_provider_session_enabled:
            return None
        token = self._bearer_token(headers)
        if token is None:
            return None
        request = {"method": method.upper(), "path": path}
        try:
            payload = self._introspect_provider_session(token)
        except RuntimeError as exc:
            self._record_provider_session_decision(
                token=token,
                payload={},
                permission=permission,
                request=request,
                status="error",
                reason=str(exc),
            )
            raise
        if payload.get("active") is not True:
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason="provider session token is inactive",
            )
            raise PermissionError("provider session token is inactive")
        try:
            expires_at = self._provider_session_expires_at(payload)
        except PermissionError as exc:
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason=str(exc),
            )
            raise
        if expires_at is not None and expires_at <= datetime.now(UTC):
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason="provider session token is expired",
                expires_at=expires_at,
            )
            raise PermissionError("provider session token is expired")
        try:
            self._ensure_provider_session_not_revoked(token, payload)
        except PermissionError as exc:
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason=str(exc),
                expires_at=expires_at,
            )
            raise
        groups = self._provider_session_groups(payload)
        role = self._provider_session_role(groups)
        if role not in ROLE_PERMISSIONS:
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason=f"unknown provider session default role: {role}",
                groups=groups,
                role=role,
                expires_at=expires_at,
            )
            raise PermissionError(f"unknown provider session default role: {role}")
        if not self._role_allows(role, permission):
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason=f"role {role} lacks permission {permission}",
                groups=groups,
                role=role,
                expires_at=expires_at,
            )
            raise PermissionError(f"role {role} lacks permission {permission}")
        try:
            user_id = self._provider_session_user_id(payload)
        except PermissionError as exc:
            self._record_provider_session_decision(
                token=token,
                payload=payload,
                permission=permission,
                request=request,
                status="denied",
                reason=str(exc),
                groups=groups,
                role=role,
                expires_at=expires_at,
            )
            raise
        self._record_provider_session_decision(
            token=token,
            payload=payload,
            permission=permission,
            request=request,
            status="accepted",
            reason="provider session accepted",
            groups=groups,
            role=role,
            user_id=user_id,
            expires_at=expires_at,
        )
        return AuthContext(
            enabled=True,
            authenticated=True,
            role=role,
            user_id=user_id,
            permission=permission,
            auth_mode="provider_session",
            identity_source=self.settings.auth_provider_session_token_header,
            groups=tuple(groups),
            session_expires_at=expires_at.isoformat() if expires_at else None,
        )

    def record_provider_session_revocation(
        self,
        request: ProviderSessionRevocationRequest,
    ) -> dict:
        now = datetime.now(UTC)
        revocations = self.list_provider_session_revocations(include_expired=False)
        token_sha256 = self._normalize_token_sha256(request.token_sha256)
        record = {
            "schema_version": "provider_session_revocation.v1",
            "revocation_id": self._revocation_id(
                token_sha256=token_sha256,
                jti=request.jti,
                subject=request.subject,
            ),
            "token_sha256": token_sha256,
            "jti": request.jti,
            "subject": request.subject,
            "reason": request.reason,
            "created_at": now.isoformat(),
            "created_by": request.user_id or "system",
            "expires_at": request.expires_at.isoformat() if request.expires_at else None,
        }
        revocations = [
            item for item in revocations if item.get("revocation_id") != record["revocation_id"]
        ]
        revocations.append(record)
        revocations.sort(key=lambda item: str(item.get("created_at", "")))
        self._write_provider_session_revocations(revocations)
        return record

    def list_provider_session_revocations(self, include_expired: bool = False) -> list[dict]:
        path = self._provider_session_revocation_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("revocations", [])
        if not isinstance(records, list):
            return []
        revocations = [item for item in records if isinstance(item, dict)]
        if include_expired:
            return revocations
        now = datetime.now(UTC)
        return [item for item in revocations if not self._revocation_expired(item, now)]

    def list_provider_session_decisions(self, limit: int | None = None) -> list[dict]:
        path = self._provider_session_decision_log_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("decisions", [])
        if not isinstance(records, list):
            return []
        decisions = [item for item in records if isinstance(item, dict)]
        decisions.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        max_records = limit or self.settings.auth_provider_session_decision_log_limit
        return decisions[:max_records]

    def _trusted_identity_context(
        self,
        permission: str,
        headers: Headers,
    ) -> AuthContext | None:
        if not self.settings.auth_trusted_identity_enabled:
            return None
        user_id = (headers.get(self.settings.auth_trusted_identity_header) or "").strip()
        if not user_id:
            return None
        groups = self._trusted_groups(headers)
        role = self._trusted_role(groups)
        if role not in ROLE_PERMISSIONS:
            raise PermissionError(f"unknown trusted identity default role: {role}")
        if not self._role_allows(role, permission):
            raise PermissionError(f"role {role} lacks permission {permission}")
        return AuthContext(
            enabled=True,
            authenticated=True,
            role=role,
            user_id=user_id,
            permission=permission,
            auth_mode="trusted_identity",
            identity_source=self.settings.auth_trusted_identity_header,
            groups=tuple(groups),
        )

    def _bearer_token(self, headers: Headers) -> str | None:
        value = headers.get(self.settings.auth_provider_session_token_header)
        if not value:
            return None
        if self.settings.auth_provider_session_token_header.lower() == "authorization":
            scheme, _, token = value.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise PermissionError("provider session requires bearer authorization")
            return token.strip()
        return value.strip() or None

    def _introspect_provider_session(self, token: str) -> dict:
        if not self.settings.auth_provider_session_introspection_url:
            raise RuntimeError(
                "DIALECTICORE_AUTH_PROVIDER_SESSION_INTROSPECTION_URL is required "
                "when provider sessions are enabled"
            )
        data = {"token": token}
        client_id = self.secret_resolver.resolve(
            self.settings.auth_provider_session_client_id_reference
        )
        client_secret = self.secret_resolver.resolve(
            self.settings.auth_provider_session_client_secret_reference
        )
        if bool(client_id) != bool(client_secret):
            raise RuntimeError(
                "provider session client ID and secret references must both resolve"
            )
        if client_id and client_secret:
            data["client_id"] = client_id
            data["client_secret"] = client_secret
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.settings.auth_provider_session_timeout_seconds,
            ) as client:
                response = client.post(
                    self.settings.auth_provider_session_introspection_url,
                    data=data,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError("provider session introspection failed") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("provider session introspection returned invalid payload")
        return payload

    def _ensure_provider_session_not_revoked(self, token: str, payload: dict) -> None:
        token_sha256 = self._hash_token(token)
        subject = self._payload_string(payload, "sub")
        jti = self._payload_string(payload, "jti")
        now = datetime.now(UTC)
        changed = False
        active_revocations = []
        for revocation in self.list_provider_session_revocations(include_expired=True):
            if self._revocation_expired(revocation, now):
                changed = True
                continue
            active_revocations.append(revocation)
            if (
                (revocation.get("token_sha256") and revocation.get("token_sha256") == token_sha256)
                or (revocation.get("jti") and revocation.get("jti") == jti)
                or (revocation.get("subject") and revocation.get("subject") == subject)
            ):
                raise PermissionError("provider session token is revoked")
        if changed:
            self._write_provider_session_revocations(active_revocations)

    def _provider_session_user_id(self, payload: dict) -> str:
        for claim in (
            self.settings.auth_provider_session_user_claim,
            "sub",
            "preferred_username",
            "email",
        ):
            value = payload.get(claim)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise PermissionError("provider session has no usable user identity")

    def _provider_session_groups(self, payload: dict) -> list[str]:
        value = payload.get(self.settings.auth_provider_session_groups_claim)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [
                group.strip()
                for token in value.split(",")
                for group in token.split(";")
                if group.strip()
            ]
        return []

    def _provider_session_role(self, groups: list[str]) -> str:
        group_role_map = self._provider_session_group_role_map()
        for group in groups:
            mapped_role = group_role_map.get(group)
            if mapped_role:
                return mapped_role
        return self.settings.auth_provider_session_default_role.strip().lower()

    def _provider_session_group_role_map(self) -> dict[str, str]:
        return self._parse_group_role_map(self.settings.auth_provider_session_group_role_map)

    def _provider_session_expires_at(self, payload: dict) -> datetime | None:
        exp = payload.get("exp")
        if exp is None:
            return None
        try:
            return datetime.fromtimestamp(int(exp), tz=UTC)
        except (TypeError, ValueError, OSError):
            raise PermissionError("provider session has invalid expiry") from None

    def _provider_session_revocation_path(self) -> Path:
        configured = self.settings.auth_provider_session_revocation_path
        if configured:
            return Path(configured).expanduser()
        return (
            Path(self.settings.runtime_state_path).expanduser()
            / "auth"
            / "provider-session-revocations.json"
        )

    def _provider_session_decision_log_path(self) -> Path:
        configured = self.settings.auth_provider_session_decision_log_path
        if configured:
            return Path(configured).expanduser()
        return (
            Path(self.settings.runtime_state_path).expanduser()
            / "auth"
            / "provider-session-decisions.json"
        )

    def _record_provider_session_decision(
        self,
        *,
        token: str,
        payload: dict,
        permission: str,
        request: dict,
        status: Literal["accepted", "denied", "error"],
        reason: str,
        groups: list[str] | None = None,
        role: str | None = None,
        user_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC)
        token_sha256 = self._hash_token(token)
        subject = self._payload_string(payload, "sub")
        jti = self._payload_string(payload, "jti")
        record = {
            "schema_version": "provider_session_decision.v1",
            "decision_id": self._provider_session_decision_id(
                token_sha256=token_sha256,
                created_at=now.isoformat(),
                permission=permission,
                status=status,
            ),
            "created_at": now.isoformat(),
            "status": status,
            "reason": reason,
            "token_sha256": token_sha256,
            "subject": subject,
            "jti": jti,
            "user_id": user_id,
            "role": role,
            "groups": groups if groups is not None else self._provider_session_groups(payload),
            "permission": permission,
            "request": request,
            "provider_active": (
                payload.get("active") if isinstance(payload.get("active"), bool) else None
            ),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        try:
            decisions = self.list_provider_session_decisions(
                limit=self.settings.auth_provider_session_decision_log_limit
            )
            decisions.append(record)
            decisions.sort(key=lambda item: str(item.get("created_at", "")))
            decisions = decisions[-self.settings.auth_provider_session_decision_log_limit :]
            self._write_provider_session_decisions(decisions)
        except OSError:
            return

    def _write_provider_session_decisions(self, decisions: list[dict]) -> None:
        path = self._provider_session_decision_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "provider_session_decision_log.v1",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "retention_limit": self.settings.auth_provider_session_decision_log_limit,
                    "decisions": decisions,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _provider_session_decision_id(
        self,
        *,
        token_sha256: str,
        created_at: str,
        permission: str,
        status: str,
    ) -> str:
        key = json.dumps(
            {
                "created_at": created_at,
                "permission": permission,
                "status": status,
                "token_sha256": token_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "session-decision-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def _write_provider_session_revocations(self, revocations: list[dict]) -> None:
        path = self._provider_session_revocation_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "provider_session_revocation_registry.v1",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "revocations": revocations,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _revocation_expired(self, revocation: dict, now: datetime) -> bool:
        expires_at = revocation.get("expires_at")
        if not isinstance(expires_at, str) or not expires_at:
            return False
        try:
            return datetime.fromisoformat(expires_at) <= now
        except ValueError:
            return False

    def _revocation_id(
        self,
        token_sha256: str | None,
        jti: str | None,
        subject: str | None,
    ) -> str:
        key = json.dumps(
            {
                "token_sha256": token_sha256,
                "jti": jti,
                "subject": subject,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "revocation-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def _normalize_token_sha256(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized.startswith("sha256:"):
            normalized = normalized.removeprefix("sha256:")
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("token_sha256 must be a SHA-256 hex digest")
        return "sha256:" + normalized

    def _hash_token(self, token: str) -> str:
        return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _payload_string(self, payload: dict, claim: str) -> str | None:
        value = payload.get(claim)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _expected_api_key(self) -> str:
        if not self.settings.auth_api_key_reference:
            raise RuntimeError(
                "DIALECTICORE_AUTH_API_KEY_REFERENCE is required when auth is enabled"
            )
        return self.secret_resolver.resolve(self.settings.auth_api_key_reference) or ""

    def _role_allows(self, role: str, permission: str) -> bool:
        permissions = ROLE_PERMISSIONS[role]
        return "*" in permissions or permission in permissions

    def _trusted_groups(self, headers: Headers) -> list[str]:
        raw_groups = headers.get(self.settings.auth_trusted_groups_header) or ""
        groups = [
            group.strip()
            for token in raw_groups.split(",")
            for group in token.split(";")
            if group.strip()
        ]
        return groups

    def _trusted_role(self, groups: list[str]) -> str:
        group_role_map = self._trusted_group_role_map()
        for group in groups:
            mapped_role = group_role_map.get(group)
            if mapped_role:
                return mapped_role
        return self.settings.auth_trusted_default_role.strip().lower()

    def _trusted_group_role_map(self) -> dict[str, str]:
        return self._parse_group_role_map(self.settings.auth_trusted_group_role_map)

    def _parse_group_role_map(self, value: str) -> dict[str, str]:
        role_rank = {
            "viewer": 0,
            "reviewer": 1,
            "editor": 2,
            "producer": 3,
            "admin": 4,
        }
        parsed: dict[str, str] = {}
        for item in value.split(","):
            if not item.strip():
                continue
            if "=" not in item:
                continue
            group, role = item.split("=", 1)
            group = group.strip()
            role = role.strip().lower()
            if not group or role not in ROLE_PERMISSIONS:
                continue
            existing_role = parsed.get(group)
            if existing_role and role_rank[existing_role] >= role_rank[role]:
                continue
            parsed[group] = role
        return parsed

    def _is_media_route(self, route: str) -> bool:
        return any(
            token in route
            for token in {
                "/localize",
                "/audio-assets/",
                "/visual-assets/",
                "/subtitles/",
                "/timeline",
                "/renders",
                "/thumbnails/",
                "/youtube-package/",
                "/production-manifest",
            }
        )

    def _is_configuration_route(self, route: str) -> bool:
        prefixes = {
            "/projects",
            "/model-endpoints",
            "/voicebox-endpoints",
            "/voice-profiles",
            "/comfyui-endpoints",
            "/comfyui-workflows",
            "/discussion-prompt-templates",
            "/visual-profiles",
            "/participant-profiles",
        }
        return any(route.startswith(prefix) for prefix in prefixes)
