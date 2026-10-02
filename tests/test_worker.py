import os
import threading

from store import FakeStore
from user import User
from verification import Metrics, TokenService
from worker import (InspectionWorker, TRIGGER_MANUAL, TRIGGER_SCHEDULED,
                    write_decisions_markdown)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def seed_store():
    store = FakeStore()
    clock = Clock(1000.0)
    service = TokenService(store, clock=clock, ttl=10, metrics=Metrics())

    fresh = User("", "Fresh", "User", "fresh@example.com")
    expired = User("", "Expired", "User", "expired@example.com")
    redeemed = User("", "Redeemed", "User", "redeemed@example.com")
    for user in (fresh, expired, redeemed):
        store.insert_user(user.dict())

    service.issue(fresh, ttl=100000)   # still valid at sweep time 2000

    service.issue(expired, ttl=10)     # expires 1010
    service.issue(redeemed, ttl=10)
    clock.value = 1009.0               # redeem one second before the boundary
    service.redeem(store.find_user_by_id(redeemed.id).confirmation_token)

    clock.value = 2000.0          # expired user's token now stale
    return store, clock, service.metrics, fresh, expired, redeemed


def test_manual_sweep_clears_expired_and_redeemed_only(tmp_path):
    store, clock, metrics, fresh, expired, redeemed = seed_store()
    worker = InspectionWorker(store, metrics=metrics, clock=clock,
                              reports_dir=str(tmp_path))

    result = worker.sweep()

    assert result.expired_cleaned == 1
    assert result.redeemed_cleaned == 1

    assert store.find_user_by_id(expired.id).confirmation_token == ""
    assert store.find_user_by_id(redeemed.id).confirmation_token == ""
    # Fresh token survives the sweep.
    assert store.find_user_by_id(fresh.id).confirmation_token != ""


def test_run_once_writes_openable_html_and_decisions(tmp_path):
    store, clock, metrics, *_ = seed_store()
    worker = InspectionWorker(store, metrics=metrics, clock=clock,
                              reports_dir=str(tmp_path))

    path = worker.run_once(TRIGGER_MANUAL)

    assert os.path.exists(path)
    content = open(path, encoding="utf-8").read()
    assert content.startswith("<!DOCTYPE html>")
    # The three trade-offs are all present in the report page.
    assert "separate collection" in content
    assert "UTC" in content
    assert "inline" in content and "redirect" in content
    # Outcome counters rendered.
    assert "Activated (success)" in content

    latest = os.path.join(str(tmp_path), "latest.html")
    assert os.path.exists(latest)
    decisions = os.path.join(str(tmp_path), "design-decisions.md")
    assert os.path.exists(decisions)


def test_scheduled_mode_runs_until_stopped(tmp_path):
    store, clock, metrics, *_ = seed_store()
    worker = InspectionWorker(store, metrics=metrics, clock=clock,
                              reports_dir=str(tmp_path))
    stop_event = threading.Event()

    produced = []

    def run():
        # Patch wait timing by driving schedule with a tiny interval and
        # stopping after two sweeps from a helper thread.
        produced.extend(worker.schedule(0.01, stop_event))

    thread = threading.Thread(target=run)
    thread.start()
    stop_event.wait(0.0)
    # Let two sweeps land, then stop.
    thread.join(0.05)
    stop_event.set()
    thread.join(1.0)

    assert len(produced) >= 1
    assert os.path.exists(produced[0])


def test_cumulative_metrics_track_runs(tmp_path):
    store, clock, metrics, *_ = seed_store()
    worker = InspectionWorker(store, metrics=metrics, clock=clock,
                              reports_dir=str(tmp_path))
    worker.run_once(TRIGGER_MANUAL)
    worker.run_once(TRIGGER_SCHEDULED)
    snap = metrics.snapshot()
    assert snap["runs"] == 2
    # Second run finds nothing left to clean.
    assert snap["expired_swept"] == 1
    assert snap["redeemed_swept"] == 1
