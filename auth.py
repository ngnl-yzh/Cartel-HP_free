import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
TOKEN_TTL = 60 * 60 * 24


def create_token() -> str:
    payload = {
        "exp": (datetime.utcnow() + timedelta(seconds=TOKEN_TTL)).isoformat(),
        "nonce": secrets.token_hex(16),
    }
    data = json.dumps(payload, separators=(",", ":"))
    sig = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{data}.{sig}".encode()).decode()


def verify_token(token: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        # 마지막 '.' 기준으로 data와 sig 분리
        last_dot = decoded.rfind(".")
        if last_dot == -1:
            return False
        data, sig = decoded[:last_dot], decoded[last_dot + 1:]
        expected = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(data)
        exp = datetime.fromisoformat(payload["exp"])
        return datetime.utcnow() < exp
    except Exception:
        return False
