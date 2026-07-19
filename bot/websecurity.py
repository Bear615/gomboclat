"""Security primitives for the web control hub.

Stdlib-only and I/O-free so everything here is directly unit-testable without
aiohttp or Discord:

  * password hashing — scrypt with a per-password random salt; the stored
    string is ``scrypt:N:r:p:salt_hex:hash_hex`` (never the plaintext),
  * session tokens — HMAC-SHA256-signed values with an embedded expiry,
  * CSRF tokens — deterministically derived from the session token, so every
    state-changing request must echo a value only a logged-in page can know,
  * ``LoginThrottle`` — a sliding-window failed-login limiter per client.

All comparisons use ``hmac.compare_digest`` (constant-time).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable

# scrypt cost parameters (interactive-login grade; ~16 MiB, tens of ms).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

MIN_PASSWORD_LENGTH = 8


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """Hash a password for storage. Uses ':' as the separator (never '$') so
    the value survives .env round-trips without shell/dotenv interpolation."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. False on any malformed input."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split(":")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Sessions + CSRF
# --------------------------------------------------------------------------- #


def generate_secret() -> str:
    """A fresh signing secret (for WEB_SESSION_SECRET)."""
    return secrets.token_urlsafe(32)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(secret: str, lifetime_seconds: int, now: float | None = None) -> str:
    """Mint a session token: ``v1.<expiry_unix>.<nonce>.<signature>``."""
    now = time.time() if now is None else now
    payload = f"v1.{int(now + lifetime_seconds)}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_sign(secret, payload)}"


def check_token(secret: str, token: str, now: float | None = None) -> bool:
    """True only for an untampered, unexpired token signed with ``secret``."""
    now = time.time() if now is None else now
    parts = token.split(".") if token else []
    if len(parts) != 4 or parts[0] != "v1":
        return False
    payload = ".".join(parts[:3])
    if not hmac.compare_digest(_sign(secret, payload), parts[3]):
        return False
    try:
        return now < int(parts[1])
    except ValueError:
        return False


def csrf_for(secret: str, token: str) -> str:
    """The CSRF token paired with a session token (deterministic per session)."""
    return hmac.new(secret.encode(), b"csrf:" + token.encode(), hashlib.sha256).hexdigest()


def check_csrf(secret: str, token: str, csrf: str) -> bool:
    return bool(csrf) and hmac.compare_digest(csrf_for(secret, token), csrf)


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #


class LoginThrottle:
    """Sliding-window failed-login limiter, keyed by client address.

    After ``max_failures`` failures within ``window`` seconds, further attempts
    are refused until the oldest failure ages out. A successful login clears
    the key. The clock is injectable for tests.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.window = window
        self._clock = clock
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window
        kept = [t for t in self._failures.get(key, []) if t > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def retry_after(self, key: str) -> float:
        """Seconds until this key may attempt a login again (0 = allowed now)."""
        now = self._clock()
        recent = self._recent(key, now)
        if len(recent) < self.max_failures:
            return 0.0
        return max(0.0, recent[0] + self.window - now)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        self._failures.setdefault(key, []).append(now)
        self._recent(key, now)

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
