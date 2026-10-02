"""Email confirmation token lifecycle.

The lifecycle is a small state machine driven by the inspection worker in
``run.py``. Every transition is projected onto the registration activation
fields of the user record (see ``user.User``) and rendered by
``templates/confirm.html``:

    ISSUE   -> confirmation_token + token_issued_at + token_expires_at set
    REDEEM  -> verified=True, token_redeemed_at set (exactly once)
    EXPIRE  -> now > token_expires_at (UTC, strict greater-than) blocks redeem
    CLEANUP -> worker strips expired tokens and redeemed residue

There is deliberately no separate token collection: the token and its UTC
expiry stamp are embedded on the user document. Resending therefore rotates
the old link, a double click is idempotent while the redeemed residue is
retained, and the worker sweep is the only "orphan" reaper.
"""

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import configparser
import os
import secrets
import threading

# Configuration
config = configparser.ConfigParser()
config.read(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'configuration.ini'))
default = config.defaults()
SECRET_KEY = os.environ.get('SECRET_KEY', default.get('SECRET_KEY', 'change-me'))
SECURITY_PASSWORD_SALT = os.environ.get(
    'SECURITY_PASSWORD_SALT', default.get('SECURITY_PASSWORD_SALT', 'salt'))

DEFAULT_TTL_SECONDS = 3600

# Lifecycle outcomes. Success and the three explicit failure classes must stay
# distinct so the confirm page and the worker report can count them apart.
ACTIVATED = "activated"
ALREADY_ACTIVE = "already_active"
EXPIRED = "expired"
INVALID = "invalid"
USER_NOT_FOUND = "user_not_found"

FAILURE_OUTCOMES = (ALREADY_ACTIVE, EXPIRED, INVALID, USER_NOT_FOUND)

# Failure presentation strategies.
DISPOSITION_INLINE = "inline"      # render confirm.html with an error branch
DISPOSITION_REDIRECT = "redirect"  # 302 to /login?reason=...


def _serializer():
    return URLSafeTimedSerializer(SECRET_KEY)


def generate_confirmation_token(email):
    """Sign an email plus a random nonce into an opaque URL-safe token.

    The nonce guarantees two tokens issued within the same second (e.g. a
    quick resend) are distinct, so a resend always rotates the old link.
    """
    payload = {"email": email, "nonce": secrets.token_urlsafe(12)}
    return _serializer().dumps(payload, salt=SECURITY_PASSWORD_SALT)


def read_token_email(token):
    """Return the signed email, or None if the signature is forged/garbled.

    ``max_age`` is intentionally not enforced here: expiry is decided by the
    embedded ``token_expires_at`` UTC stamp on the user record, never by the
    serializer's own clock, so both use one comparison in one timezone.
    """
    try:
        payload = _serializer().loads(token, salt=SECURITY_PASSWORD_SALT)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(payload, dict):
        return payload.get("email")
    # Tokens minted by older versions carried a bare email string.
    return payload


def confirm_token(token, expiration=DEFAULT_TTL_SECONDS):
    """Backward-compatible helper retained for older callers."""
    try:
        payload = _serializer().loads(
            token, salt=SECURITY_PASSWORD_SALT, max_age=expiration)
    except Exception:
        return False
    return payload["email"] if isinstance(payload, dict) else payload


class Metrics:
    """Thread-safe counters for confirmation attempts and worker sweeps.

    Failure attempts are split by presentation path (inline confirm page vs
    redirect to login) so the HTML report can tell the two paths apart.
    """

    OUTCOMES = (ACTIVATED, ALREADY_ACTIVE, EXPIRED, INVALID, USER_NOT_FOUND)

    def __init__(self):
        self._lock = threading.Lock()
        self.attempts = {outcome: 0 for outcome in self.OUTCOMES}
        self.inline_failures = 0
        self.redirect_failures = 0
        self.expired_swept = 0
        self.redeemed_swept = 0
        self.runs = 0

    def record_attempt(self, outcome, disposition):
        with self._lock:
            self.attempts[outcome] += 1
            if outcome in FAILURE_OUTCOMES:
                if disposition == DISPOSITION_REDIRECT:
                    self.redirect_failures += 1
                else:
                    self.inline_failures += 1

    def record_sweep(self, expired_cleaned, redeemed_cleaned):
        with self._lock:
            self.expired_swept += expired_cleaned
            self.redeemed_swept += redeemed_cleaned
            self.runs += 1

    def snapshot(self):
        with self._lock:
            return {
                "attempts": dict(self.attempts),
                "total_attempts": sum(self.attempts.values()),
                "inline_failures": self.inline_failures,
                "redirect_failures": self.redirect_failures,
                "expired_swept": self.expired_swept,
                "redeemed_swept": self.redeemed_swept,
                "runs": self.runs,
            }


class TokenService:
    """Projects the token state machine onto an injectable user store."""

    def __init__(self, store, clock=None, ttl=DEFAULT_TTL_SECONDS, metrics=None):
        self.store = store
        self.clock = clock or _utc_now
        self.ttl = ttl
        self.metrics = metrics

    def issue(self, user, ttl=None):
        """ISSUE transition: embed a fresh token and UTC expiry stamp.

        Reissuing rotates the link: the previous token is overwritten, so an
        older email link can no longer match any user record.
        """
        ttl = self.ttl if ttl is None else ttl
        now = self.clock()
        token = generate_confirmation_token(user.email)
        self.store.set_confirmation_token(
            user.id, token, issued_at=now, expires_at=now + ttl)
        return token

    def redeem(self, token, disposition=DISPOSITION_INLINE):
        """REDEEM / EXPIRE classification for one confirmation click."""
        email = read_token_email(token)
        if email is None:
            outcome = INVALID
        else:
            user = self.store.find_user_by_token(token)
            if user is not None:
                if user.verified or user.token_redeemed_at:
                    outcome = ALREADY_ACTIVE
                elif self.clock() > user.token_expires_at:
                    # Strict greater-than in UTC: at exactly token_expires_at
                    # the link is still valid for one final second.
                    outcome = EXPIRED
                elif self.store.activate_user(user.id, token, self.clock()):
                    outcome = ACTIVATED
                else:
                    # Lost the atomic race against a concurrent click.
                    outcome = ALREADY_ACTIVE
            else:
                owner = self.store.find_user_by_email(email)
                if owner is None:
                    outcome = USER_NOT_FOUND
                elif owner.verified:
                    outcome = ALREADY_ACTIVE
                else:
                    # Link was rotated by a resend, or expired and swept.
                    outcome = INVALID

        if self.metrics is not None:
            self.metrics.record_attempt(outcome, disposition)
        return outcome


def _utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
