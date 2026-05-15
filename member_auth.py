"""회원 인증 유틸리티 — PBKDF2 비밀번호 해시 + HMAC 토큰"""
import hashlib
import hmac
import os
import time
from typing import Optional

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
TOKEN_TTL = 60 * 60 * 24 * 7  # 7일


# ── 비밀번호 ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, dk_hex = stored.split(":", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def verify_team_password(password: str, stored: str) -> bool:
    """TeamMember 비밀번호 검증 — PBKDF2 우선, 레거시 SHA-256 fallback 포함"""
    try:
        salt, dk_hex = stored.split(":", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        if hmac.compare_digest(dk.hex(), dk_hex):
            return True
        # 마이그레이션 기간 호환: 기존 SHA-256 단순 해시
        legacy_h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
        return hmac.compare_digest(legacy_h, dk_hex)
    except Exception:
        return False


# ── 토큰 ────────────────────────────────────────────────────────────────────

def create_member_token(member_id: int) -> str:
    expires_at = int(time.time()) + TOKEN_TTL
    payload = f"{member_id}:{expires_at}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_member_token(token: str) -> Optional[int]:
    """유효하면 member_id 반환, 아니면 None"""
    try:
        payload, sig = token.rsplit(".", 1)
        member_id_str, expires_at_str = payload.split(":", 1)
        if time.time() > int(expires_at_str):
            return None
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return int(member_id_str)
    except Exception:
        return None
