import sys
import threading

import pytest

import run as run_module
from store import FakeStore
from verification import (ACTIVATED, ALREADY_ACTIVE, DISPOSITION_INLINE,
                           DISPOSITION_REDIRECT, EXPIRED, INVALID,
                           USER_NOT_FOUND)


class Clock:
    def __init__(self, value=10_000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def env():
    clock = Clock()
    store = FakeStore()
    app = run_module.create_app(store=store, clock=clock, token_ttl=3600,
                                failure_disposition=DISPOSITION_INLINE)
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()
    return {"app": app, "client": client, "store": store, "clock": clock}


def register(client, email="ada@example.com"):
    return client.post("/register", data={
        "email": email, "title": "Ms",
        "first_name": "Ada", "last_name": "Lovelace", "pass": "s3cret!pw",
        "confirmation": "s3cret!pw",
    }, follow_redirects=False)


def current_token(store, email="ada@example.com"):
    return store.find_user_by_email(email).confirmation_token


def test_register_issues_embedded_token(env):
    response = register(env["client"])
    assert response.status_code == 302

    user = env["store"].find_user_by_email("ada@example.com")
    assert user.confirmation_token
    assert user.token_expires_at > user.token_issued_at
    assert user.verified is False


def test_confirm_activates_and_renders_success(env):
    register(env["client"])
    token = current_token(env["store"])

    response = env["client"].get("/confirm/" + token)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "verified successfully" in body
    assert env["store"].find_user_by_email("ada@example.com").verified is True


def test_concurrent_double_click_activates_once(env):
    register(env["client"])
    token = current_token(env["store"])

    statuses = []

    def click():
        statuses.append(env["client"].get("/confirm/" + token).status_code)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(200) == 8
    metrics = env["app"].extensions["metrics"].snapshot()
    assert metrics["attempts"][ACTIVATED] == 1
    assert metrics["attempts"][ALREADY_ACTIVE] == 7


def test_expired_link_shows_inline_expired_branch(env):
    register(env["client"])
    token = current_token(env["store"])
    env["clock"].value += 10_000  # past ttl

    response = env["client"].get("/confirm/" + token)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "has expired" in body
    assert env["app"].extensions["metrics"].snapshot()["attempts"][EXPIRED] == 1


def test_garbled_link_shows_inline_invalid_branch(env):
    response = env["client"].get("/confirm/garbage-token")
    body = response.get_data(as_text=True)
    assert "could not be verified" in body
    assert env["app"].extensions["metrics"].snapshot()["attempts"][INVALID] == 1


def test_signed_token_without_user_shows_not_found(env):
    from verification import generate_confirmation_token
    token = generate_confirmation_token("nobody@example.com")
    response = env["client"].get("/confirm/" + token)
    body = response.get_data(as_text=True)
    assert "No account found" in body
    assert env["app"].extensions["metrics"].snapshot()[
        "attempts"][USER_NOT_FOUND] == 1


def test_redirect_disposition_sends_failures_to_login(tmp_path):
    clock = Clock()
    store = FakeStore()
    app = run_module.create_app(store=store, clock=clock,
                                failure_disposition=DISPOSITION_REDIRECT)
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()
    register(client)
    token = current_token(store)
    clock.value += 10_000  # expire

    response = client.get("/confirm/" + token)

    assert response.status_code == 302
    assert "/login?reason=expired" in response.headers["Location"]

    follow = client.get(response.headers["Location"])
    assert "Email confirmation failed" in follow.get_data(as_text=True)

    metrics = app.extensions["metrics"].snapshot()
    assert metrics["redirect_failures"] == 1
    assert metrics["inline_failures"] == 0


def test_inline_vs_redirect_paths_counted_separately(tmp_path):
    clock = Clock()
    inline_app = run_module.create_app(
        store=FakeStore(), clock=clock,
        failure_disposition=DISPOSITION_INLINE)
    redirect_app = run_module.create_app(
        store=FakeStore(), clock=clock,
        failure_disposition=DISPOSITION_REDIRECT)
    inline_app.config['WTF_CSRF_ENABLED'] = False
    redirect_app.config['WTF_CSRF_ENABLED'] = False

    inline_app.test_client().get("/confirm/nope")
    redirect_app.test_client().get("/confirm/nope")

    assert inline_app.extensions["metrics"].snapshot()["inline_failures"] == 1
    assert redirect_app.extensions["metrics"].snapshot()[
        "redirect_failures"] == 1


def test_cli_manual_inspect_produces_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A fresh app with no users still yields a double-click-openable report.
    exit_code = run_module.main(["--inspect", "--reports-dir", "reports"])
    assert exit_code == 0

    reports = tmp_path / "reports"
    html_files = list(reports.glob("inspection-*.html"))
    assert html_files
    assert (reports / "latest.html").exists()
    assert (reports / "design-decisions.md").exists()
