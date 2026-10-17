"""Authentication helpers: password verification, session tokens, CSRF."""

import hmac
import secrets
import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD_HASH, SESSION_MAX_AGE

_serializer = URLSafeTimedSerializer(SECRET_KEY)
_SESSION_SALT = "session"
_CSRF_SALT = "csrf"


def verify_password(username, password):
    """Constant-time check of username + password against stored hash."""
    if not hmac.compare_digest(username, ADMIN_USERNAME):
        # Still run bcrypt to prevent timing oracle
        bcrypt.hashpw(b"dummy", bcrypt.gensalt())
        return False
    return bcrypt.checkpw(
        password.encode("utf-8"),
        ADMIN_PASSWORD_HASH.encode("utf-8"),
    )


def create_session_token(username):
    return _serializer.dumps(username, salt=_SESSION_SALT)


def validate_session_token(token):
    """Return username if valid, else None."""
    try:
        return _serializer.loads(token, salt=_SESSION_SALT, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def generate_csrf_token(session_token):
    """Derive a CSRF token from the session so it's tied to the user."""
    return _serializer.dumps(session_token, salt=_CSRF_SALT)


def validate_csrf_token(csrf_token, session_token):
    try:
        value = _serializer.loads(csrf_token, salt=_CSRF_SALT, max_age=SESSION_MAX_AGE)
        return hmac.compare_digest(value, session_token)
    except (BadSignature, SignatureExpired):
        return False
