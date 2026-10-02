import run
from events import InMemoryEventLog
from store import FakeDataStore
from user import User
from verification import utcnow


def test_worker_once_generates_report(tmp_path):
    store = FakeDataStore()
    event_log = InMemoryEventLog()
    now = utcnow()

    user = User("", "Old", "Token", "old@example.com")
    store.insert_one("users", user.dict())
    run.worker.issue_token(
        store, user, ttl_seconds=-100, event_log=event_log, now=now)

    result, path = run.run_worker_once(
        store, event_log, str(tmp_path))
    assert result.expired_tokens == 1
    assert path.startswith(str(tmp_path))
    assert (tmp_path / "latest.html").exists()


def test_seed_demo_and_manual_worker(tmp_path):
    store = FakeDataStore()
    event_log = InMemoryEventLog()
    run.seed_demo_data(store, event_log)
    result, path = run.run_worker_once(
        store, event_log, str(tmp_path), failure_mode=run.worker.INLINE)
    # Demo seeds one expired token and one confirmed residue record.
    assert result.expired_tokens == 1
    assert result.confirmed_residue == 1
    content = open(path, encoding="utf-8").read()
    assert "令牌生命周期巡检报告" in content


def test_main_worker_command_runs_once(tmp_path, monkeypatch):
    exit_code = run.main([
        "worker", "--seed-demo", "--reports-dir", str(tmp_path)])
    assert exit_code == 0
    assert (tmp_path / "latest.html").exists()


def test_main_worker_scheduled_mode(tmp_path, monkeypatch):
    calls = []

    class StopLoop(Exception):
        pass

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= 2:
            raise StopLoop

    monkeypatch.setattr(run.time, "sleep", fake_sleep)
    try:
        run.main(["worker", "--seed-demo", "--interval", "1",
                  "--reports-dir", str(tmp_path)])
    except StopLoop:
        pass
    assert calls == [1, 1]
