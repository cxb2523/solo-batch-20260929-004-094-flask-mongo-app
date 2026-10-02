import events
import report
import worker
from store import FakeDataStore
from user import User
from verification import utcnow


def build_log():
    store = FakeDataStore()
    event_log = events.InMemoryEventLog()
    now = utcnow()

    user = User("", "A", "B", "a@example.com")
    store.insert_one("users", user.dict())
    token, _, _ = worker.issue_token(store, user, event_log=event_log, now=now)
    worker.attempt_confirmation(
        store, token, path=worker.INLINE, event_log=event_log, now=now)
    worker.attempt_confirmation(
        store, token, path=worker.INLINE, event_log=event_log, now=now)
    worker.attempt_confirmation(
        store, "bad", path=worker.REDIRECT, event_log=event_log, now=now)
    return store, event_log


def test_report_written_and_self_contained(tmp_path):
    store, event_log = build_log()
    result = worker.sweep(store, event_log=event_log)
    path = report.write_report(str(tmp_path), result, event_log)

    assert path.endswith(".html")
    content = open(path, encoding="utf-8").read()
    assert "<html" in content and "<style>" in content
    # No external resource references -> opens offline by double click.
    assert "http://" not in content and "https://" not in content
    assert (tmp_path / "latest.html").exists()


def test_report_documents_three_tradeoffs(tmp_path):
    store, event_log = build_log()
    result = worker.sweep(store, event_log=event_log)
    content = report.render_html(result, event_log, worker.INLINE, utcnow())
    assert "内嵌" in content
    assert "UTC" in content
    assert "内联" in content and "重定向" in content


def test_report_distinguishes_paths_and_outcomes(tmp_path):
    store, event_log = build_log()
    result = worker.sweep(store, event_log=event_log)
    content = report.render_html(result, event_log, worker.REDIRECT, utcnow())
    stats = events.aggregate(event_log.events())
    assert stats["reason_paths"][f"{worker.SUCCESS}|{worker.INLINE}"] == 1
    assert stats["reason_paths"][
        f"{worker.ALREADY_ACTIVE}|{worker.INLINE}"] == 1
    assert stats["reason_paths"][
        f"{worker.INVALID}|{worker.REDIRECT}"] == 1
    # Matrix renders both path columns for each outcome.
    assert "内联路径" in content and "重定向 login 路径" in content
