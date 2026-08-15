from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SECRET_NAME = "dialecticore_api_key"
DEFAULT_SECRET_BYTES = 48


@dataclass(frozen=True)
class AdminBootstrapResult:
    secret_path: Path
    docker_secret_reference: str
    api_key_header: str
    role_header: str
    user_header: str
    role: str
    user: str
    secret: str | None = None


def create_admin_api_key_secret(
    secrets_dir: Path,
    *,
    secret_name: str = DEFAULT_SECRET_NAME,
    token_bytes: int = DEFAULT_SECRET_BYTES,
    force: bool = False,
    show_secret: bool = False,
) -> AdminBootstrapResult:
    normalized_name = _secret_name(secret_name)
    if token_bytes < 32:
        raise ValueError("admin API key token must use at least 32 random bytes")
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = secrets_dir / normalized_name
    token = secrets.token_urlsafe(token_bytes)
    flags = os.O_WRONLY | os.O_CREAT
    if force:
        flags |= os.O_TRUNC
    else:
        flags |= os.O_EXCL
    try:
        fd = os.open(secret_path, flags, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(
            f"{secret_path} already exists; pass --force to rotate the admin API key"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n")
    os.chmod(secret_path, stat.S_IRUSR | stat.S_IWUSR)
    return AdminBootstrapResult(
        secret_path=secret_path,
        docker_secret_reference=f"docker-secret:{normalized_name}",
        api_key_header="x-dialecticore-api-key",
        role_header="x-dialecticore-role",
        user_header="x-dialecticore-user",
        role="admin",
        user="bootstrap-admin",
        secret=token if show_secret else None,
    )


def format_admin_bootstrap_result(result: AdminBootstrapResult) -> str:
    lines = [
        f"secret_path={result.secret_path}",
        f"auth_api_key_reference={result.docker_secret_reference}",
        f"api_key_header={result.api_key_header}",
        f"role_header={result.role_header}",
        f"user_header={result.user_header}",
        f"role={result.role}",
        f"user={result.user}",
    ]
    if result.secret is not None:
        lines.append(f"api_key={result.secret}")
    else:
        lines.append("api_key=<redacted>")
    return "\n".join(lines)


def _secret_name(value: str) -> str:
    name = value.strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("secret name must be a single Docker secret filename")
    return name


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the initial DialectiCore admin API-key secret file.",
    )
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=Path("secrets"),
        help="Directory used by docker-compose.production-secrets.yml.",
    )
    parser.add_argument(
        "--secret-name",
        default=DEFAULT_SECRET_NAME,
        help="Docker secret filename to create.",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=DEFAULT_SECRET_BYTES,
        help="Random byte count before URL-safe encoding; minimum 32.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the existing secret file to rotate the admin API key.",
    )
    parser.add_argument(
        "--show-secret",
        action="store_true",
        help="Print the generated API key once for manual curl/browser setup.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = create_admin_api_key_secret(
            args.secrets_dir,
            secret_name=args.secret_name,
            token_bytes=args.bytes,
            force=args.force,
            show_secret=args.show_secret,
        )
    except (OSError, ValueError) as exc:
        print(f"admin bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(format_admin_bootstrap_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
