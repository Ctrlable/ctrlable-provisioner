"""
JWT-based authentication for the Ctrlable Provisioner API.

If ADMIN_PASSWORD_HASH or JWT_SECRET are not set in .env the API runs
in open mode (no auth) with a startup warning — preserves backward
compatibility for existing deployments. Set both to enable auth.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

if TYPE_CHECKING:
    from .config import Settings

_bearer = HTTPBearer(auto_error=False)

# Fallback secret used when JWT_SECRET is not configured.
# Tokens will be invalidated on every restart — acceptable for open-mode.
_EPHEMERAL_SECRET = secrets.token_hex(32)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_token(username: str, settings: "Settings") -> str:
    secret = settings.jwt_secret or _EPHEMERAL_SECRET
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    return jwt.encode({"sub": username, "exp": exp}, secret, algorithm="HS256")


def _decode(token: str, secret: str) -> str:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        sub = payload.get("sub")
        if not sub:
            raise ValueError
        return sub
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def auth_enabled(settings: "Settings") -> bool:
    return True  # auth is always on; default admin/admin with forced password change


def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    """FastAPI dependency — validates Bearer token. Always enforced."""
    from .config import get_settings
    settings = get_settings()

    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _decode(creds.credentials, settings.jwt_secret)
