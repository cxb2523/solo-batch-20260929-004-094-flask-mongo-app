from datetime import timedelta

from verification import (
    generate_confirmation_token,
    is_expired,
    issue_expiry,
    read_token,
    utcnow,
)


def test_token_roundtrip():
    token = generate_confirmation_token("a@example.com")
    assert read_token(token) == "a@example.com"


def test_garbage_token_is_invalid():
    assert read_token("not-a-real-token") is None


def test_expiry_boundary_is_strict_greater_than():
    now = utcnow()
    # At the exact expiry second the link is still valid (now == expires).
    assert is_expired(now, now=now) is False
    assert is_expired(now, now=now + timedelta(seconds=1)) is True
    assert is_expired(now, now=now - timedelta(seconds=1)) is False


def test_issue_expiry_uses_ttl():
    now = utcnow()
    expires = issue_expiry(60, now=now)
    assert expires == now + timedelta(seconds=60)


def test_naive_local_datetime_is_treated_as_utc():
    from datetime import datetime

    aware_now = utcnow()
    naive_stamp = aware_now.replace(tzinfo=None)
    assert is_expired(naive_stamp, now=aware_now) is False
    assert is_expired(naive_stamp, now=aware_now + timedelta(seconds=1)) is True
