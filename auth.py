import hashlib
import hmac
import os
import time

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
TOKEN_TTL = 60 * 60 * 24


def create_token() -> str:
    expires_at = int(time.time()) + TOKEN_TTL
    payload = str(expires_at)
    signature = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_token(token: str) -> bool:
    try:
        expires_at, signature = token.split(".", 1)
        if time.time() > int(expires_at):
            return False
        expected = hmac.new(SECRET_KEY.encode(), expires_at.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False
