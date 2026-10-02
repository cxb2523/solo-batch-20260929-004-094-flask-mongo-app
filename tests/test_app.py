import events
import worker
from run import create_app
from store import FakeDataStore
from user import User
from verification import utcnow


def make_client(failure_mode=worker.INLINE):
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    app = create_app(
        store, event_log,
        {"WTF_CSRF_ENABLED": False, "TESTING": True,
         "CONFIRM_FAILURE_MODE": failure_mode})
    return app.test_client(), store, event_log


def register(store, email="new@example.com"):
    user = User("", "New", "User", email)
    store.insert_one("users", user.dict())
    token, _, _ = worker.issue_token(store, user)
    return user, token


def test_valid_confirm_inlines_success():
    client, store, event_log = make_client()
    _user, token = register(store)
    response = client.get(f"/confirm/{token}")
    assert response.status_code == 200
    assert b"verified successfully" in response.data
    record = store.find_one("users", {"email": "new@example.com"})
    assert record["verified"] is True


def test_double_click_through_route_only_activates_once():
    client, store, _log = make_client()
    _user, token = register(store)
    first = client.get(f"/confirm/{token}")
    second = client.get(f"/confirm/{token}")
    assert first.status_code == 200
    assert b"Already verified" in second.data
    stats = events.aggregate(_log.events())
    assert stats["reason_paths"][f"{worker.SUCCESS}|{worker.INLINE}"] == 1
    assert stats["reason_paths"][
        f"{worker.ALREADY_ACTIVE}|{worker.INLINE}"] == 1


def test_invalid_token_inlines_error():
    client, _store, _log = make_client()
    response = client.get("/confirm/garbage.token")
    assert response.status_code == 200
    assert b"Invalid link" in response.data


def test_expired_token_inlines_expired():
    client, store, _log = make_client()
    user = User("", "E", "X", "exp@example.com")
    store.insert_one("users", user.dict())
    token, _, _ = worker.issue_token(store, user, ttl_seconds=-10)
    response = client.get(f"/confirm/{token}")
    assert b"Link expired" in response.data


def test_missing_user_inlines_not_found():
    client, _store, _log = make_client()
    from verification import generate_confirmation_token
    token = generate_confirmation_token("ghost@example.com")
    response = client.get(f"/confirm/{token}")
    assert b"Account not found" in response.data


def test_redirect_mode_sends_to_login_with_code():
    client, store, _log = make_client(failure_mode=worker.REDIRECT)
    response = client.get("/confirm/garbage.token")
    assert response.status_code == 302
    assert "/login?confirm_error=invalid_token" in response.headers["Location"]

    user = User("", "E", "X", "exp2@example.com")
    store.insert_one("users", user.dict())
    token, _, _ = worker.issue_token(store, user, ttl_seconds=-10)
    response = client.get(f"/confirm/{token}")
    assert "confirm_error=expired" in response.headers["Location"]


def test_redirect_login_page_renders_confirm_error():
    client, _store, _log = make_client(failure_mode=worker.REDIRECT)
    response = client.get("/login?confirm_error=no_user")
    assert b"No account matches" in response.data


def test_register_issues_embedded_token_and_runs_offline():
    client, store, _log = make_client()
    response = client.post("/register", data={
        "email": "fresh@example.com", "title": "Ms",
        "first_name": "Fresh", "last_name": "User", "pass": "secret123"})
    assert response.status_code == 302
    record = store.find_one("users", {"email": "fresh@example.com"})
    assert record["confirm_token"]
    assert record["confirm_expires_at"] is not None
    assert record["verified"] is False


def test_resend_rotates_old_link():
    client, store, _log = make_client()
    user, old_token = register(store, "resend@example.com")
    new_token, _, _ = worker.issue_token(store, user)
    response = client.get(f"/confirm/{old_token}")
    assert b"Invalid link" in response.data
    response = client.get(f"/confirm/{new_token}")
    assert b"verified successfully" in response.data


def test_offline_app_starts_without_mongo_env():
    app = create_app(config={"WTF_CSRF_ENABLED": False, "TESTING": True})
    assert app.test_client().get("/").status_code == 200
