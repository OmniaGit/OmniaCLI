"""HTTP Basic authentication with PBKDF2 password hashes."""

from __future__ import annotations

import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"{ALGORITHM}${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
    except ValueError:
        return False
    return secrets.compare_digest(digest.hex(), expected)


class Authenticator:
    """Check Basic credentials against the configured users.

    The browser sends the credentials with every request (pages, job polling,
    log stream), so a successful PBKDF2 check is remembered for the life of
    the process, keyed by a keyed hash of the password, never the password.
    """

    def __init__(self, users: dict[str, str]):
        self.users = users
        self._key = secrets.token_bytes(32)
        self._verified: dict[str, bytes] = {}
        self._dummy = hash_password(secrets.token_hex(8))

    def _fingerprint(self, username: str, password: str) -> bytes:
        return hmac.new(self._key, f"{username}\0{password}".encode(), "sha256").digest()

    def check(self, username: str, password: str) -> bool:
        encoded = self.users.get(username)
        if encoded is None:
            # Spend the time of a real check so usernames cannot be probed
            verify_password(password, self._dummy)
            return False
        fingerprint = self._fingerprint(username, password)
        cached = self._verified.get(username)
        if cached is not None and secrets.compare_digest(cached, fingerprint):
            return True
        if verify_password(password, encoded):
            self._verified[username] = fingerprint
            return True
        return False
