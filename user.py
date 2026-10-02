import uuid


# User class
class User():
    def __init__(self, title, first_name, last_name, email, id="", verified=False,
                 confirm_token=None, confirm_expires_at=None, confirmed_at=None):
        # Main initialiser
        self.title = title if title != "none" else ""
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.id = uuid.uuid4().hex if not id else id
        self.verified = verified
        # Registration activation fields. The confirmation token and its UTC
        # expiry stamp are embedded directly on the user record (no separate
        # token collection). ``confirmed_at`` is set when the token is redeemed.
        self.confirm_token = confirm_token
        self.confirm_expires_at = confirm_expires_at
        self.confirmed_at = confirmed_at

    @classmethod
    def make_from_dict(cls, d):
        # Initialise User object from a dictionary
        return cls(
            d['title'], d['first_name'], d['last_name'], d['email'], d['id'],
            d.get('verified', False),
            d.get('confirm_token'),
            d.get('confirm_expires_at'),
            d.get('confirmed_at')
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
            "confirm_token": self.confirm_token,
            "confirm_expires_at": self.confirm_expires_at,
            "confirmed_at": self.confirmed_at
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
