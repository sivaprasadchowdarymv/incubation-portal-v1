"""Authentication, role checks and file-name safety."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

ROLES = ("student", "reviewer", "admin")
PBKDF2_ITER = 200_000


class AuthError(PermissionError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    team_id: int | None = None


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def require_role(user: User | None, *roles: str) -> User:
    if user is None:
        raise AuthError("Authentication required")
    if user.role not in roles:
        raise AuthError(f"Role '{user.role}' is not permitted for this action")
    return user


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, max_len: int = 120) -> str:
    """Strip any directory component and unsafe characters (prevents path traversal)."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = _SAFE.sub("_", base).lstrip(".") or "upload"
    stem, dot, ext = base.rpartition(".")
    if dot and len(base) > max_len:
        base = stem[: max_len - len(ext) - 1] + "." + ext
    return base[:max_len]
