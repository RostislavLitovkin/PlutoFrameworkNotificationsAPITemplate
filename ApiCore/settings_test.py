"""
Settings for running the test suite without external services.

ApiCore.settings reads configuration and builds PLAY_INTEGRITY_CONFIG eagerly at
import time, so importing it with an empty environment raises. The variables it
needs are therefore seeded first, with throwaway values in the shapes pyattest
demands: base64 for the decryption key, a base64 DER public key for the
verification key, and a hex digest for the app signing key. None of them are
exercised by these tests, which never run attestation.

The database is swapped for in-memory SQLite so tests need neither a PostgreSQL
server nor credentials.

Usage:
    python manage.py test --settings=ApiCore.settings_test
"""
import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_throwaway_der = (
    ec.generate_private_key(ec.SECP256R1())
    .public_key()
    .public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
)

os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("SECRET_KEY", "test-only-key-not-used-in-production")
os.environ.setdefault("DEBUG", "0")
os.environ.setdefault("APK_NAME", "com.example.test")
os.environ.setdefault("APP_ATTEST_APP_ID", "TEAMID.com.example.test")
os.environ.setdefault(
    "GOOGLE_PLAY_INTEGRITY_DECRYPTION_KEY",
    base64.standard_b64encode(bytes(32)).decode(),
)
os.environ.setdefault(
    "GOOGLE_PLAY_INTEGRITY_VERIFICATION_KEY",
    base64.standard_b64encode(_throwaway_der).decode(),
)
os.environ.setdefault("GOOGLE_PLAY_INTEGRITY_APP_SIGNING_KEY", "AB" * 32)

# Imported after the environment is seeded, so this cannot move to the top.
from ApiCore.settings import *  # noqa: E402,F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# WhiteNoise warns on every request because collectstatic has not run; it serves no
# purpose in API tests. Dropped so genuine warnings stand out.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m]  # noqa: F405

# Tests deliberately assert on 4xx responses, and Django logs each one as a warning.
LOGGING = {  # noqa: F811
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "ERROR"},
    "loggers": {"django.request": {"handlers": ["console"], "level": "ERROR"}},
}

# No Firebase credentials are configured in tests, so FCM topic subscription and
# message sending fail. Production code already logs and swallows subscription
# failures; tests that care about delivery patch that boundary explicitly.
