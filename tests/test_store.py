from datetime import datetime, timezone

from store import FakeDataStore


def test_insert_find_one_and_projection():
    store = FakeDataStore()
    store.insert_one("users", {"email": "a@x.com", "secret": 1})
    found = store.find_one("users", {"email": "a@x.com"})
    assert found["email"] == "a@x.com"
    assert store.find_one("users", {"email": "missing"}) is None


def test_projection_excludes_field():
    store = FakeDataStore()
    store.insert_one("users", {"email": "a@x.com", "_id": 42})
    found = store.find_one("users", {"email": "a@x.com"}, {"_id": 0})
    assert "_id" not in found


def test_update_one_set_and_unset():
    store = FakeDataStore()
    store.insert_one("users", {"email": "a@x.com", "verified": False})
    result = store.update_one(
        "users", {"email": "a@x.com"}, {"$set": {"verified": True}})
    assert result.matched_count == 1
    assert store.find_one("users", {"email": "a@x.com"})["verified"] is True
    store.update_one("users", {"email": "a@x.com"},
                     {"$unset": {"verified": ""}})
    assert "verified" not in store.find_one("users", {"email": "a@x.com"})


def test_ne_and_or_operators():
    store = FakeDataStore()
    store.insert_one("users", {"id": "1", "email": "a@x.com"})
    store.insert_one("users", {"id": "2", "email": "b@x.com"})
    others = list(store.find("users", {"id": {"$ne": "1"}}))
    assert len(others) == 1 and others[0]["id"] == "2"
    match = list(store.find(
        "users", {"$or": [{"id": "1"}, {"id": "2"}]}))
    assert len(match) == 2


def test_find_sorts_descending():
    store = FakeDataStore()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.insert_one("notes", {"id": "1", "timestamp": base})
    store.insert_one("notes", {"id": "2", "timestamp": base.replace(day=2)})
    rows = list(store.find("notes", {}).sort("timestamp", -1))
    assert [row["id"] for row in rows] == ["2", "1"]


def test_conditional_update_only_matches_once():
    store = FakeDataStore()
    store.insert_one("users", {
        "email": "a@x.com", "confirm_token": "t", "verified": False})
    update = {"$set": {"verified": True},
              "$unset": {"confirm_token": "", "confirm_expires_at": ""}}
    first = store.update_one(
        "users",
        {"email": "a@x.com", "confirm_token": "t", "verified": False}, update)
    second = store.update_one(
        "users",
        {"email": "a@x.com", "confirm_token": "t", "verified": False}, update)
    assert first.matched_count == 1
    assert second.matched_count == 0


def test_delete_many_or_query():
    store = FakeDataStore()
    store.insert_one("messages", {"from_id": "u", "to_id": "x"})
    store.insert_one("messages", {"from_id": "y", "to_id": "u"})
    result = store.delete_many(
        "messages", {"$or": [{"from_id": "u"}, {"to_id": "u"}]})
    assert result.deleted_count == 2
