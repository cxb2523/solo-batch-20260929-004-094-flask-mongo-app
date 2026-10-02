"""Injectable data stores.

The app talks to the store through a small interface, so a fake in-memory
implementation can be injected for tests and offline runs (no MongoDB, no
network). :class:`FakeStore` is the default. :class:`MongoStore` is an
optional adapter for a real deployment and is only constructed when a Mongo
URI is present.

All mutating token operations are atomic under a lock, which is what makes a
concurrent double click on the same link take effect exactly once.
"""

import threading

from user import User


class UserStore:
    """Store interface used by the token lifecycle and the Flask routes."""

    def insert_user(self, user_dict):
        raise NotImplementedError

    def find_user_by_email(self, email):
        raise NotImplementedError

    def find_user_by_id(self, user_id):
        raise NotImplementedError

    def find_user_by_token(self, token):
        raise NotImplementedError

    def all_users(self):
        raise NotImplementedError

    def set_confirmation_token(self, user_id, token, issued_at, expires_at):
        raise NotImplementedError

    def activate_user(self, user_id, token):
        """Atomically redeem ``token``. Return True only on the first redeem.

        The compare-and-swap on the embedded token plus ``verified`` is the
        idempotency guard: a second concurrent click with the same token sees
        ``verified`` already true and gets False back.
        """
        raise NotImplementedError

    def clear_token_fields(self, user_id):
        raise NotImplementedError


class FakeStore(UserStore):
    """Thread-safe in-memory store. Works fully offline."""

    def __init__(self):
        self._users = {}
        self._lock = threading.RLock()

    def insert_user(self, user_dict):
        with self._lock:
            self._users[user_dict['id']] = dict(user_dict)
        return True

    @staticmethod
    def _to_user(record):
        return User.make_from_dict(record) if record is not None else None

    def find_user_by_email(self, email):
        with self._lock:
            for record in self._users.values():
                if record.get('email') == email:
                    return self._to_user(dict(record))
        return None

    def find_user_by_id(self, user_id):
        with self._lock:
            record = self._users.get(user_id)
            return self._to_user(dict(record)) if record else None

    def find_user_by_token(self, token):
        with self._lock:
            for record in self._users.values():
                if record.get('confirmation_token') == token:
                    return self._to_user(dict(record))
        return None

    def all_users(self):
        with self._lock:
            return [self._to_user(dict(record)) for record in self._users.values()]

    def set_confirmation_token(self, user_id, token, issued_at, expires_at):
        with self._lock:
            record = self._users[user_id]
            record['confirmation_token'] = token
            record['token_issued_at'] = issued_at
            record['token_expires_at'] = expires_at
            record['token_redeemed_at'] = 0.0

    def activate_user(self, user_id, token, now=None):
        with self._lock:
            record = self._users.get(user_id)
            if record is None:
                return False
            if record.get('verified'):
                return False
            if record.get('confirmation_token') != token:
                return False
            now = now if now is not None else _epoch_now()
            record['verified'] = True
            record['token_redeemed_at'] = now
            # Token string is retained as redeemed residue so an immediate
            # double click is recognisable and idempotent until the sweep.
            return True

    def clear_token_fields(self, user_id):
        with self._lock:
            record = self._users.get(user_id)
            if record is None:
                return
            record['confirmation_token'] = ""
            record['token_issued_at'] = 0.0
            record['token_expires_at'] = 0.0
            record['token_redeemed_at'] = 0.0


class MongoStore(UserStore):
    """Optional adapter for a real MongoDB deployment.

    Implements the same interface. Redeem uses a single conditional
    ``update_one`` so the compare-and-swap happens atomically on the server.
    """

    def __init__(self, mongo):
        self._users = mongo.db.users

    @staticmethod
    def _to_user(record):
        if record is None:
            return None
        record.pop('_id', None)
        return User.make_from_dict(record)

    def insert_user(self, user_dict):
        return self._users.insert_one(dict(user_dict)) is not None

    def find_user_by_email(self, email):
        return self._to_user(self._users.find_one({'email': email}, {'_id': 0}))

    def find_user_by_id(self, user_id):
        return self._to_user(self._users.find_one({'id': user_id}, {'_id': 0}))

    def find_user_by_token(self, token):
        return self._to_user(
            self._users.find_one({'confirmation_token': token}, {'_id': 0}))

    def all_users(self):
        return [self._to_user(record)
                for record in self._users.find({}, {'_id': 0})]

    def set_confirmation_token(self, user_id, token, issued_at, expires_at):
        self._users.update_one(
            {'id': user_id},
            {'$set': {
                'confirmation_token': token,
                'token_issued_at': issued_at,
                'token_expires_at': expires_at,
                'token_redeemed_at': 0.0,
            }})

    def activate_user(self, user_id, token, now=None):
        result = self._users.update_one(
            {'id': user_id, 'confirmation_token': token, 'verified': False},
            {'$set': {'verified': True,
                      'token_redeemed_at': now if now is not None
                      else _epoch_now()}})
        return result.modified_count == 1

    def clear_token_fields(self, user_id):
        self._users.update_one(
            {'id': user_id},
            {'$set': {
                'confirmation_token': "",
                'token_issued_at': 0.0,
                'token_expires_at': 0.0,
                'token_redeemed_at': 0.0,
            }})


def _epoch_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
