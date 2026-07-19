"""Tests for the web hub's security primitives (bot/websecurity.py) and the
incremental event buffer the browser polls (bot/web.py)."""

from __future__ import annotations

from bot.websecurity import (
    LoginThrottle,
    check_csrf,
    check_token,
    csrf_for,
    generate_secret,
    hash_password,
    issue_token,
    verify_password,
)


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def test_password_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)


def test_password_wrong_rejected():
    stored = hash_password("correct horse battery staple")
    assert not verify_password("Tr0ub4dor&3", stored)
    assert not verify_password("", stored)


def test_password_hash_is_salted_and_not_plaintext():
    a = hash_password("hunter22222")
    b = hash_password("hunter22222")
    assert a != b  # per-password random salt
    assert "hunter22222" not in a
    assert a.startswith("scrypt:")
    assert "$" not in a  # survives .env / shell round-trips untouched


def test_password_malformed_stored_values_rejected():
    for bad in ("", "nonsense", "scrypt:1:2", "md5:aa:bb:cc:dd:ee", "scrypt:x:y:z:zz:zz"):
        assert not verify_password("anything", bad)


# --------------------------------------------------------------------------- #
# Session tokens + CSRF
# --------------------------------------------------------------------------- #

SECRET = generate_secret()


def test_token_valid_within_lifetime():
    token = issue_token(SECRET, 3600, now=1000.0)
    assert check_token(SECRET, token, now=1000.0)
    assert check_token(SECRET, token, now=4599.0)


def test_token_expires():
    token = issue_token(SECRET, 3600, now=1000.0)
    assert not check_token(SECRET, token, now=4601.0)


def test_token_tamper_rejected():
    token = issue_token(SECRET, 3600, now=1000.0)
    parts = token.split(".")
    # Extend the expiry without re-signing.
    forged = ".".join(["v1", "99999999999", parts[2], parts[3]])
    assert not check_token(SECRET, forged, now=1000.0)


def test_token_wrong_secret_rejected():
    token = issue_token(SECRET, 3600)
    assert not check_token(generate_secret(), token)


def test_token_garbage_rejected():
    for bad in ("", "v1", "a.b.c.d", "v2.123.nonce.sig", None):
        assert not check_token(SECRET, bad or "")


def test_csrf_matches_only_its_session():
    token_a = issue_token(SECRET, 3600)
    token_b = issue_token(SECRET, 3600)
    csrf_a = csrf_for(SECRET, token_a)
    assert check_csrf(SECRET, token_a, csrf_a)
    assert not check_csrf(SECRET, token_b, csrf_a)
    assert not check_csrf(SECRET, token_a, "")
    assert not check_csrf(SECRET, token_a, "deadbeef")


# --------------------------------------------------------------------------- #
# Login throttle
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_throttle_allows_until_limit_then_blocks():
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=300, clock=clock)
    key = "1.2.3.4"
    for _ in range(3):
        assert throttle.retry_after(key) == 0.0
        throttle.record_failure(key)
    assert throttle.retry_after(key) > 0.0


def test_throttle_window_slides_open_again():
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=300, clock=clock)
    key = "1.2.3.4"
    for _ in range(3):
        throttle.record_failure(key)
    clock.t = 299.0
    assert throttle.retry_after(key) > 0.0
    clock.t = 301.0
    assert throttle.retry_after(key) == 0.0


def test_throttle_success_clears_failures():
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=300, clock=clock)
    key = "1.2.3.4"
    for _ in range(3):
        throttle.record_failure(key)
    throttle.record_success(key)
    assert throttle.retry_after(key) == 0.0


def test_throttle_keys_are_independent():
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=3, window=300, clock=clock)
    for _ in range(3):
        throttle.record_failure("attacker")
    assert throttle.retry_after("attacker") > 0.0
    assert throttle.retry_after("someone-else") == 0.0


# --------------------------------------------------------------------------- #
# Event buffer (imported from bot.web — also smoke-tests the module import)
# --------------------------------------------------------------------------- #


def test_event_buffer_incremental_polling():
    from bot.web import EventBuffer

    buf = EventBuffer(maxlen=100)
    buf.append("state", "one")
    buf.append("audit-ok", "two")
    events = buf.since(0)
    assert [e["text"] for e in events] == ["one", "two"]
    last = events[-1]["id"]
    assert buf.since(last) == []
    buf.append("maint", "three")
    assert [e["text"] for e in buf.since(last)] == ["three"]


def test_event_buffer_bounded_but_ids_keep_growing():
    from bot.web import EventBuffer

    buf = EventBuffer(maxlen=5)
    for i in range(12):
        buf.append("state", f"msg {i}")
    events = buf.since(0)
    assert len(events) == 5
    assert [e["text"] for e in events] == [f"msg {i}" for i in range(7, 12)]
    assert events[-1]["id"] == 12
