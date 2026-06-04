"""极简鉴权 —— 课程项目演示用。

- 用 SHA-256 存密码
- token = base64(user_id:hmac)，HMAC 使用启动时随机生成的服务端 secret
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import User


# 启动时随机一次（重启失效，足够用于课程演示）
_SECRET = secrets.token_bytes(32)


def hash_password(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)


def make_token(user_id: int) -> str:
    payload = str(user_id).encode("utf-8")
    sig = hmac.new(_SECRET, payload, hashlib.sha256).digest()
    raw = payload + b":" + base64.urlsafe_b64encode(sig).rstrip(b"=")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: str) -> Optional[int]:
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + pad).encode("ascii"))
        payload, sig_b64 = raw.split(b":", 1)
        expected_sig = hmac.new(_SECRET, payload, hashlib.sha256).digest()
        sig = base64.urlsafe_b64decode(sig_b64 + b"=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return int(payload.decode("utf-8"))
    except Exception:
        return None


def current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    token = authorization.split(" ", 1)[1].strip()
    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def admin_required(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return user
