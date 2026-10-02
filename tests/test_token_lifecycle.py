import threading

import pytest

from store import FakeStore
from user import User
from verification import (ACTIVATED, ALREADY_ACTIVE, EXPIRED, INVALID,
                           USER_NOT_FOUND, Metrics, TokenService)


def make_user(store, email="a@example.com"):
    user = User("", "Ada", "Lovelace", email)
    store.insert_user(user.dict())
    return user


class ControllableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def test_issue_embeds_token_and_utc_expiry_stamp():
    store = FakeStore()
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=3600)
    user = make_user(store)

    token = service.issue(user)

    stored = store.find_user_by_id(user.id)
    assert stored.confirmation_token == token
    assert stored.token_issued_at == 1000.0
    assert stored.token_expires_at == 4600.0
    assert stored.verified is False


def test_redeem_activates_exactly_once():
    store = FakeStore()
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=3600)
    user = make_user(store)
    token = service.issue(user)

    assert service.redeem(token) == ACTIVATED
    assert store.find_user_by_id(user.id).verified is True

    # Immediate second click is idempotent, not a second activation.
    assert service.redeem(token) == ALREADY_ACTIVE


def test_concurrent_double_click_activates_once():
    store = FakeStore()
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=3600)
    user = make_user(store)
    token = service.issue(user)

    outcomes = []

    def click():
        outcomes.append(service.redeem(token))

    threads = [threading.Thread(target=click) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(ACTIVATED) == 1
    assert outcomes.count(ALREADY_ACTIVE) == 9


def test_boundary_strict_greater_than():
    store = FakeStore()
    # expires_at = 1000 + 10 = 1010
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=10)
    user = make_user(store)
    token = service.issue(user)

    # One instant before the boundary: valid.
    clock.value = 1009.0
    assert service.redeem(token) == ACTIVATED


def test_boundary_exactly_equal_is_still_valid():
    store = FakeStore()
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=10)
    user = make_user(store)
    token = service.issue(user)

    # now == expires_at: strict greater-than means still valid for that second.
    clock.value = 1010.0
    assert service.redeem(token) == ACTIVATED


def test_expired_one_second_after_boundary():
    store = FakeStore()
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=10)
    user = make_user(store)
    token = service.issue(user)

    clock.value = 1011.0
    assert service.redeem(token) == EXPIRED
    assert store.find_user_by_id(user.id).verified is False


def test_garbled_token_is_invalid():
    service = TokenService(FakeStore())
    assert service.redeem("not-a-real-token") == INVALID


def test_valid_token_for_unknown_email_is_user_not_found():
    store = FakeStore()
    service = TokenService(store)
    # A correctly signed token but no matching user record.
    from verification import generate_confirmation_token
    token = generate_confirmation_token("ghost@example.com")
    assert service.redeem(token) == USER_NOT_FOUND


def test_resend_rotates_old_link():
    store = FakeStore()
    clock = ControllableClock(1000.0)
    service = TokenService(store, clock=clock, ttl=3600)
    user = make_user(store)
    old_token = service.issue(user)

    clock.value = 2000.0
    new_token = service.issue(user)

    assert new_token != old_token
    stored = store.find_user_by_id(user.id)
    assert stored.confirmation_token == new_token

    # The old link can no longer match any embedded token.
    assert service.redeem(old_token) == INVALID
    # The rotated link still works.
    assert service.redeem(new_token) == ACTIVATED


def test_metrics_split_outcomes_and_dispositions():
    metrics = Metrics()
    metrics.record_attempt(ACTIVATED, "inline")
    metrics.record_attempt(EXPIRED, "inline")
    metrics.record_attempt(ALREADY_ACTIVE, "redirect")
    snap = metrics.snapshot()
    assert snap["attempts"][ACTIVATED] == 1
    assert snap["attempts"][EXPIRED] == 1
    assert snap["inline_failures"] == 1
    assert snap["redirect_failures"] == 1
