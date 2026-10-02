"""Token lifecycle worker: issue / redeem / expire / sweep.

The state machine lives entirely on the user record:

    (no fields)            -- issue -->   confirm_token + confirm_expires_at
    token + unverified     -- redeem -->  verified=True, confirmed_at set
    token + unverified     -- expire -->  fields unset (back to clean)
    verified + stale token -- sweep  -->  residue fields unset

Redemption uses a single conditional update
``{email, confirm_token, verified: False}`` so a concurrent double click on
the same link can only succeed once; the losing request sees an already
active account.
"""

from dataclasses import dataclass, field

import events
from verification import (
    TOKEN_TTL_SECONDS,
    generate_confirmation_token,
    is_expired,
    issue_expiry,
    read_token,
    utcnow,
)

USERS = "users"

# Confirmation outcomes
SUCCESS = "success"
INVALID = "invalid_token"
EXPIRED = "expired"
ALREADY_ACTIVE = "already_active"
NO_USER = "no_user"

# Rendering paths
INLINE = "inline"
REDIRECT = "redirect"


@dataclass
class SweepResult:
    started_at: object
    finished_at: object = None
    users_scanned: int = 0
    expired_tokens: int = 0
    confirmed_residue: int = 0
    expired_emails: list = field(default_factory=list)
    residue_emails: list = field(default_factory=list)

    def as_dict(self):
        return {
            "started_at": str(self.started_at),
            "finished_at": str(self.finished_at),
            "users_scanned": self.users_scanned,
            "expired_tokens": self.expired_tokens,
            "confirmed_residue": self.confirmed_residue,
            "expired_emails": list(self.expired_emails),
            "residue_emails": list(self.residue_emails),
        }


def issue_token(store, user, ttl_seconds=TOKEN_TTL_SECONDS, now=None, event_log=None):
    """Embed a freshly signed token and UTC expiry stamp on the user.

    Re-issuing rotates the token: the old link no longer matches the stored
    token and is rejected as invalid on use.
    """
    moment = now or utcnow()
    token = generate_confirmation_token(user.email)
    expires_at = issue_expiry(ttl_seconds, now=moment)
    if hasattr(user, "email"):
        user.confirm_token = token
        user.confirm_expires_at = expires_at
        user.verified = False
    existing = store.find_one(USERS, {"email": user.email}) or {}
    rotated = bool(existing.get("confirm_token"))
    store.update_one(
        USERS,
        {"email": user.email},
        {"$set": {"confirm_token": token, "confirm_expires_at": expires_at,
                  "verified": False}},
    )
    if event_log is not None:
        event_log.record(events.ISSUED, email=user.email, rotated=rotated,
                         expires_at=str(expires_at))
    return token, expires_at, rotated


def redeem_token(store, token, now=None, event_log=None):
    """Redeem a link. Returns ``(outcome, user_dict_or_None)``.

    Exactly one concurrent caller can win the conditional update.
    """
    moment = now or utcnow()
    email = read_token(token)
    if not email:
        return INVALID, None

    user = store.find_one(USERS, {"email": email}, {"_id": 0})
    if user is None:
        return NO_USER, None

    if user.get("verified"):
        return ALREADY_ACTIVE, user

    stored_token = user.get("confirm_token")
    if not stored_token or stored_token != token:
        # Rotated by a resend, or never issued / already swept.
        return INVALID, user

    if is_expired(user.get("confirm_expires_at"), now=moment):
        return EXPIRED, user

    result = store.update_one(
        USERS,
        {"email": email, "confirm_token": token, "verified": False},
        {"$set": {"verified": True, "confirmed_at": moment},
         "$unset": {"confirm_token": "", "confirm_expires_at": ""}},
    )
    if result and result.matched_count == 1:
        user = store.find_one(USERS, {"email": email}, {"_id": 0})
        if event_log is not None:
            event_log.record(events.REDEEMED, email=email, redeemed_at=str(moment))
        return SUCCESS, user

    # Lost a race with a concurrent click: the account is now active.
    user = store.find_one(USERS, {"email": email}, {"_id": 0})
    return ALREADY_ACTIVE, user


def attempt_confirmation(store, token, path, event_log=None, now=None):
    """Redeem and audit the attempt under its rendering path."""
    outcome, user = redeem_token(store, token, now=now, event_log=event_log)
    email = user.get("email") if user else None
    if event_log is not None:
        event_log.record(events.CONFIRM_ATTEMPT, outcome=outcome, path=path,
                         email=email)
    return outcome, user


def sweep(store, event_log=None, now=None):
    """Expire stale tokens and clear confirmed-token residue.

    Because tokens are embedded on the user, "orphan cleanup" only needs to
    unembed stale fields; there is no separate collection to collect.
    """
    moment = now or utcnow()
    result = SweepResult(started_at=moment)
    for user in store.find(USERS, {}):
        result.users_scanned += 1
        email = user.get("email")
        token = user.get("confirm_token")
        expires_at = user.get("confirm_expires_at")

        if not user.get("verified") and token and is_expired(expires_at, now=moment):
            store.update_one(
                USERS,
                {"email": email, "confirm_token": token},
                {"$unset": {"confirm_token": "", "confirm_expires_at": ""}},
            )
            result.expired_tokens += 1
            result.expired_emails.append(email)
            if event_log is not None:
                event_log.record(events.EXPIRED, email=email,
                                 expired_at=str(moment))

        elif user.get("verified") and token:
            store.update_one(
                USERS,
                {"email": email},
                {"$unset": {"confirm_token": "", "confirm_expires_at": ""}},
            )
            result.confirmed_residue += 1
            result.residue_emails.append(email)
            if event_log is not None:
                event_log.record(events.RESIDUE_CLEARED, email=email,
                                 cleared_at=str(moment))

    result.finished_at = utcnow()
    return result
