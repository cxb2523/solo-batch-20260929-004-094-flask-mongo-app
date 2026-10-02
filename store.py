"""Pluggable data stores behind a single collection-style interface.

The web app and the worker talk to this interface only, so the whole
application runs fully offline against :class:`FakeDataStore`.
``MongoDataStore`` is a thin adapter over a PyMongo database and is only
constructed when a real MongoDB URI is supplied.
"""

import copy
import threading
from datetime import datetime, timezone


def _matches(doc, query):
    if "$or" in query:
        return any(_matches(doc, clause) for clause in query["$or"])
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict) and any(
            operator in expected
            for operator in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in")
        ):
            for operator, operand in expected.items():
                if operator == "$eq" and not actual == operand:
                    return False
                if operator == "$ne" and not actual != operand:
                    return False
                if operator == "$gt" and not (actual is not None and actual > operand):
                    return False
                if operator == "$gte" and not (actual is not None and actual >= operand):
                    return False
                if operator == "$lt" and not (actual is not None and actual < operand):
                    return False
                if operator == "$lte" and not (actual is not None and actual <= operand):
                    return False
                if operator == "$in" and actual not in operand:
                    return False
        elif isinstance(expected, dict):
            return False
        elif actual != expected:
            return False
    return True


def _apply_update(doc, update):
    if "$set" in update:
        doc.update(update["$set"])
    if "$unset" in update:
        for field in update["$unset"]:
            doc.pop(field, None)


class UpdateResult:
    def __init__(self, matched_count, modified_count=0):
        self.matched_count = matched_count
        self.modified_count = modified_count

    def __bool__(self):
        return self.matched_count > 0


class DeleteResult:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count

    def __bool__(self):
        return True


class CollectionView:
    """Sort/projection helper returned by :meth:`FakeDataStore.find`."""

    def __init__(self, records):
        self._records = records

    def sort(self, key, direction=1):
        reverse = direction == -1
        return sorted(self._records,
                      key=lambda doc: _sort_key(doc, key, reverse),
                      reverse=reverse)

    def limit(self, count):
        return self._records[:count]

    def __iter__(self):
        return iter(self._records)


_MIN = datetime.min.replace(tzinfo=timezone.utc)
_MAX = datetime.max.replace(tzinfo=timezone.utc)


def _sort_key(doc, key, reverse):
    value = doc.get(key)
    if value is None:
        return _MAX if reverse else _MIN
    return value


class FakeDataStore:
    """Thread-safe in-memory data store; no network access required."""

    def __init__(self):
        self._collections = {}
        self._lock = threading.RLock()

    def _collection(self, name):
        return self._collections.setdefault(name, [])

    def insert_one(self, collection, document):
        with self._lock:
            self._collection(collection).append(copy.deepcopy(document))
            return True

    def find_one(self, collection, query, projection=None):
        with self._lock:
            for doc in self._collection(collection):
                if _matches(doc, query):
                    return self._project(copy.deepcopy(doc), projection)
        return None

    def find(self, collection, query, projection=None):
        with self._lock:
            records = [
                self._project(copy.deepcopy(doc), projection)
                for doc in self._collection(collection)
                if _matches(doc, query)
            ]
        return CollectionView(records)

    @staticmethod
    def _project(doc, projection):
        if not projection:
            return doc
        for field, flag in projection.items():
            if flag == 0:
                doc.pop(field, None)
        return doc

    def update_one(self, collection, query, update):
        with self._lock:
            for doc in self._collection(collection):
                if _matches(doc, query):
                    before = copy.deepcopy(doc)
                    _apply_update(doc, update)
                    modified = 1 if doc != before else 0
                    return UpdateResult(1, modified)
        return UpdateResult(0)

    def update_many(self, collection, query, update):
        with self._lock:
            matched = 0
            for doc in self._collection(collection):
                if _matches(doc, query):
                    _apply_update(doc, update)
                    matched += 1
            return UpdateResult(matched, matched)

    def delete_one(self, collection, query):
        with self._lock:
            records = self._collection(collection)
            for index, doc in enumerate(records):
                if _matches(doc, query):
                    records.pop(index)
                    return DeleteResult(1)
        return DeleteResult(0)

    def delete_many(self, collection, query):
        with self._lock:
            records = self._collection(collection)
            remaining = [doc for doc in records if not _matches(doc, query)]
            deleted = len(records) - len(remaining)
            self._collections[collection] = remaining
            return DeleteResult(deleted)

    def count(self, collection, query=None):
        with self._lock:
            return sum(
                1 for doc in self._collection(collection)
                if _matches(doc, query or {})
            )

    def all(self, collection):
        with self._lock:
            return copy.deepcopy(self._collection(collection))


class MongoCollectionAdapter:
    """Exposes the same find/find_one/update_* surface as the fake store."""

    def __init__(self, collection):
        self._collection = collection

    def find(self, query, projection=None):
        return self._collection.find(query, projection)

    def find_one(self, query, projection=None):
        return self._collection.find_one(query, projection)

    def update_one(self, query, update):
        return self._collection.update_one(query, update)

    def update_many(self, query, update):
        return self._collection.update_many(query, update)

    def insert_one(self, document):
        return self._collection.insert_one(document)

    def delete_one(self, query):
        return self._collection.delete_one(query)

    def delete_many(self, query):
        return self._collection.delete_many(query)


class MongoDataStore:
    """Adapter around a real PyMongo database.

    Importing this class never opens a connection; the database is only
    touched when :meth:`connect` is called, so the module remains safe to
    import while offline.
    """

    def __init__(self, uri, dbname):
        self._uri = uri
        self._dbname = dbname
        self._db = None

    def connect(self):
        from pymongo import MongoClient

        self._db = MongoClient(self._uri)[self._dbname]
        return self

    def _collection(self, name):
        if self._db is None:
            self.connect()
        return MongoCollectionAdapter(self._db[name])

    def insert_one(self, collection, document):
        return self._collection(collection).insert_one(document)

    def find_one(self, collection, query, projection=None):
        return self._collection(collection).find_one(query, projection)

    def find(self, collection, query, projection=None):
        return self._collection(collection).find(query, projection)

    def update_one(self, collection, query, update):
        return self._collection(collection).update_one(query, update)

    def update_many(self, collection, query, update):
        return self._collection(collection).update_many(query, update)

    def delete_one(self, collection, query):
        return self._collection(collection).delete_one(query)

    def delete_many(self, collection, query):
        return self._collection(collection).delete_many(query)

    def count(self, collection, query=None):
        return self._collection(collection).count_documents(query or {})

    def all(self, collection):
        return list(self._collection(collection).find({}))


def default_store():
    """Build a store from the environment.

    Falls back to the offline fake store whenever no MongoDB URI is set.
    """
    uri = __import__("os").environ.get("MONGODB_URI")
    dbname = __import__("os").environ.get("MONGO_DBNAME")
    if uri and dbname and not uri.startswith("<"):
        return MongoDataStore(uri, dbname)
    return FakeDataStore()
