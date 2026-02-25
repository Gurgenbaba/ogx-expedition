# app/security.py
from __future__ import annotations
import hashlib, hmac, time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import bcrypt
from jose import JWTError, jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from .models import User
from .settings import settings

CSRF_COOKIE = "ogx_csrf"
_ALG = "HS256"


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_access_token(user: User) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "usr": user.username,
        "adm": user.is_admin,
        "ver": user.token_version,
        "iat": now,
        "exp": now + settings.jwt_access_minutes * 60,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


async def require_jwt_user(
    request: Request,
    db,
    require_admin: bool = False,
) -> Tuple[Optional[User], Optional[JSONResponse]]:
    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get("ogx_token") or ""

    if not token:
        return None, JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)

    try:
        payload = jwt.decode(
            token, settings.jwt_secret,
            algorithms=[_ALG],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except JWTError:
        return None, JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)

    uid = int(payload.get("sub", 0))
    ver = int(payload.get("ver", -1))

    res = await db.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if not u or not u.is_active:
        return None, JSONResponse({"ok": False, "error": "user_not_found"}, status_code=401)
    if u.token_version != ver:
        return None, JSONResponse({"ok": False, "error": "token_revoked"}, status_code=401)
    if require_admin and not u.is_admin:
        return None, JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    return u, None
