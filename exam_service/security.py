"""Password hashing and HTTP Basic authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .db import Database


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(actual, expected)


@dataclass
class CurrentUser:
    id: int
    username: str
    role: str
    display_name: str


def _parse_basic(authorization: str | None) -> tuple[str, str]:
    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header; use HTTP Basic",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        raw = base64.b64decode(authorization.split(" ", 1)[1]).decode()
        username, password = raw.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed Basic credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return username, password


def get_current_user_factory(db: Database):
    """Build the FastAPI dependency that resolates the HTTP Basic user."""

    def authenticate(
        authorization: str | None = Header(default=None),
    ) -> CurrentUser:
        username, password = _parse_basic(authorization)
        with db.read() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role, display_name"
                " FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return CurrentUser(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            display_name=row["display_name"],
        )

    return authenticate


def require_roles_factory(authenticate):
    def require_roles(*roles: str):
        def checker(user: CurrentUser = Depends(authenticate)) -> CurrentUser:
            if user.role not in roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"role '{user.role}' may not use this endpoint;"
                    f" required one of {list(roles)}",
                )
            return user

        return checker

    return require_roles


def new_id() -> str:
    return secrets.token_hex(8)
