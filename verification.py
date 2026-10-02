import configparser
import os
import secrets
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Configuration
config = configparser.ConfigParser()
config.read('configuration.ini')
default = config['DEFAULT']
SECRET_KEY = os.environ.get('SECRET_KEY', default['SECRET_KEY'])
SECURITY_PASSWORD_SALT = os.environ.get(
    'SECURITY_PASSWORD_SALT', default['SECURITY_PASSWORD_SALT'])

# Default confirmation-token lifetime, in seconds.
TOKEN_TTL_SECONDS = 3600


def utcnow():
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_utc(value):
    """Normalise a stored datetime to timezone-aware UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def generate_confirmation_token(email):
    """Sign the email address with a fresh random nonce.

    The nonce guarantees that re-issuing within the same itsdangerous
    timestamp second still produces a new token, so a resend always
    rotates the old link. The token carries no expiry of its own; the
    lifecycle expiry lives on the user record.
    """
    serializer = URLSafeTimedSerializer(SECRET_KEY)
    payload = {"email": email, "nonce": secrets.token_urlsafe(16)}
    return serializer.dumps(payload, salt=SECURITY_PASSWORD_SALT)


def read_token(token, max_age=None):
    """Verify the signature and return the embedded email, or ``None``.

    Signature validity is independent of the lifecycle expiry, which is
    stored on the user record as a UTC stamp and evaluated by
    :func:`is_expired`. ``max_age`` optionally caps the signed payload age.
    """
    serializer = URLSafeTimedSerializer(SECRET_KEY)
    try:
        payload = serializer.loads(
            token, salt=SECURITY_PASSWORD_SALT, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(payload, dict) and "email" in payload:
        return payload["email"]
    return payload


def issue_expiry(ttl_seconds=TOKEN_TTL_SECONDS, now=None):
    """Return the UTC expiry stamp for a token issued now."""
    moment = now or utcnow()
    return moment + timedelta(seconds=ttl_seconds)


def is_expired(expires_at, now=None):
    """Strict ``now > expires`` comparison in UTC.

    Boundary semantics: the token is valid *at* the expiry second and only
    expires strictly after it. A ``>=`` comparison would make a freshly
    minted token whose stored stamp rounds to the same tick expire instantly;
    a naive local-time comparison would drift by the host UTC offset.
    """
    if expires_at is None:
        return False
    moment = now or utcnow()
    return to_utc(moment) > to_utc(expires_at)


# Backwards-compatible helper: signature + UTC stamp check in one call.
def confirm_token(token, expires_at=None, expiration=TOKEN_TTL_SECONDS):
    email = read_token(token, max_age=expiration)
    if not email:
        return False
    if expires_at is not None and is_expired(expires_at):
        return False
    return email
