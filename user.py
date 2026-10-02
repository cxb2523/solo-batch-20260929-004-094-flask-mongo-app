from datetime import datetime
import uuid


# User class
class User():
    def __init__(self, title, first_name, last_name, email, id="", verified=False,
                 confirmation_token="", token_issued_at=0.0, token_expires_at=0.0,
                 token_redeemed_at=0.0, password=""):
        # Main initialiser
        self.title = title if title != "none" else ""
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.id = uuid.uuid4().hex if not id else id
        self.verified = verified
        # Registration activation fields. The confirmation token is embedded
        # directly on the user record together with its UTC expiry stamp, so
        # the whole lifecycle (issue / redeem / expire / cleanup) lives on the
        # user document instead of a separate token collection.
        self.confirmation_token = confirmation_token
        self.token_issued_at = token_issued_at
        self.token_expires_at = token_expires_at
        # Timestamp set when the token was consumed. The token fields stay on
        # the record as "redeemed residue" until the inspection worker clears
        # them; this keeps a double click idempotent within that window.
        self.token_redeemed_at = token_redeemed_at
        # Password hash is held only long enough to authenticate a login and is
        # not exposed by dict()/the session.
        self.password = password

    @classmethod
    def make_from_dict(cls, d):
        # Initialise User object from a dictionary
        return cls(
            d['title'], d['first_name'], d['last_name'], d['email'],
            d['id'], d.get('verified', False),
            d.get('confirmation_token', ""),
            d.get('token_issued_at', 0.0),
            d.get('token_expires_at', 0.0),
            d.get('token_redeemed_at', 0.0),
            d.get('password', "")
        )

    def dict(self):
        # Return dictionary representation of the object
        return {
            "id": self.id,
            "title": self.title,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "verified": self.verified,
            "confirmation_token": self.confirmation_token,
            "token_issued_at": self.token_issued_at,
            "token_expires_at": self.token_expires_at,
            "token_redeemed_at": self.token_redeemed_at
        }

    def display_name(self):
        # Return concatenation of name components
        if self.title:
            return self.title + " " + self.first_name + " " + self.last_name
        else:
            return self.first_name + " " + self.last_name

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id


# Anonymous user class
class Anonymous():

    @property
    def is_authenticated(self):
        return False

    @property
    def is_active(self):
        return False

    @property
    def is_anonymous(self):
        return True

    def get_id(self):
        return None
