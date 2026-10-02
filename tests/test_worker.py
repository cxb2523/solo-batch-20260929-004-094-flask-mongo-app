import threading
from datetime import timedelta

import events
import worker
from store import FakeDataStore
from user import User
from verification import utcnow


def make_user(store, email="a@example.com", **kwargs):
    user = User("", "Test", "User", email, **kwargs)
    store.insert_one("users", user.dict())
    return user


def test_issue_embeds_token_and_expiry():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    user = make_user(store)
    token, expires, rotated = worker.issue_token(
        store, user, event_log=event_log)
    record = store.find_one("users", {"email": user.email})
    assert record["confirm_token"] == token
    assert record["confirm_expires_at"] is not None
    assert rotated is False


def test_resend_rotates_and_invalidates_old_link():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    user = make_user(store)
    old_token, _, rotated = worker.issue_token(
        store, user, event_log=event_log)
    assert rotated is False
    new_token, _, rotated = worker.issue_token(
        store, user, event_log=event_log)
    assert rotated is True
    assert old_token != new_token
    outcome, _ = worker.redeem_token(store, old_token, event_log=event_log)
    assert outcome == worker.INVALID

def test_redeem_success_activates_and_clears_fields():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    user = make_user(store)
    token, _, _ = worker.issue_token(store, user, event_log=event_log)
    outcome, updated = worker.redeem_token(store, token, event_log=event_log)
    assert outcome == worker.SUCCESS
    assert updated["verified"] is True
    assert updated.get("confirm_token") is None
    assert updated.get("confirmed_at") is not None


def test_double_click_is_idempotent():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    user = make_user(store)
    token, _, _ = worker.issue_token(store, user, event_log=event_log)
    outcomes = []

    def click():
        outcome, _u = worker.redeem_token(store, token, event_log=event_log)
        outcomes.append(outcome)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(worker.SUCCESS) == 1
    assert outcomes.count(worker.ALREADY_ACTIVE) == 7
    record = store.find_one("users", {"email": user.email})
    assert record["verified"] is True

def test_expired_token_is_rejected():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    now = utcnow()
    user = make_user(store)
    token, _, _ = worker.issue_token(
        store, user, ttl_seconds=-10, event_log=event_log, now=now)
    outcome, _ = worker.redeem_token(
        store, token, event_log=event_log, now=now)
    assert outcome == worker.EXPIRED


def test_token_valid_exactly_at_expiry_second():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    now = utcnow()
    user = make_user(store)
    token, expires, _ = worker.issue_token(
        store, user, ttl_seconds=0, event_log=event_log, now=now)
    # now == expires: still valid under strict greater-than semantics.
    outcome, _ = worker.redeem_token(
        store, token, event_log=event_log, now=expires)
    assert outcome == worker.SUCCESS


def test_invalid_and_missing_user_outcomes():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    outcome, user = worker.redeem_token(
        store, "garbage", event_log=event_log)
    assert outcome == worker.INVALID and user is None

    # A well-formed token for a user that no longer exists.
    from verification import generate_confirmation_token
    token = generate_confirmation_token("ghost@example.com")
    outcome, user = worker.redeem_token(store, token, event_log=event_log)
    assert outcome == worker.NO_USER and user is None


def test_attempt_confirmation_records_path():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    outcome, _ = worker.attempt_confirmation(
        store, "garbage", path=worker.REDIRECT, event_log=event_log)
    assert outcome == worker.INVALID
    event = event_log.events()[-1]
    assert event["type"] == events.CONFIRM_ATTEMPT
    assert event["outcome"] == worker.INVALID
    assert event["path"] == worker.REDIRECT


def test_sweep_clears_expired_and_residue():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    now = utcnow()

    expired_user = make_user(store, email="expired@example.com")
    worker.issue_token(store, expired_user, ttl_seconds=-3600,
                       event_log=event_log, now=now)

    active_user = make_user(store, email="active@example.com", verified=True)
    active_record = store.find_one(
        "users", {"email": "active@example.com"})
    active_record["confirm_token"] = "residue"
    active_record["confirm_expires_at"] = now - timedelta(hours=2)
    store.update_one("users", {"email": "active@example.com"},
                     {"$set": {"confirm_token": "residue",
                               "confirm_expires_at": now - timedelta(hours=2)}})

    fresh_user = make_user(store, email="fresh@example.com")
    worker.issue_token(store, fresh_user, ttl_seconds=3600,
                       event_log=event_log, now=now)

    result = worker.sweep(store, event_log=event_log, now=now)
    assert result.users_scanned == 3
    assert result.expired_tokens == 1
    assert result.confirmed_residue == 1
    assert result.expired_emails == ["expired@example.com"]

    expired_record = store.find_one(
        "users", {"email": "expired@example.com"})
    assert expired_record.get("confirm_token") is None
    active_record = store.find_one(
        "users", {"email": "active@example.com"})
    assert active_record["verified"] is True
    assert active_record.get("confirm_token") is None
    fresh_record = store.find_one("users", {"email": "fresh@example.com"})
    assert fresh_record.get("confirm_token") is not None
