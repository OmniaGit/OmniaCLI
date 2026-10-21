"""HTTP Basic authentication with PBKDF2 password hashes, bearer tokens (for
MCP clients and for linked OmniaCLI instances) stored as their SHA-256, and the
roles that decide what each caller may do."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

# user: monitors only; developer: also acts on the servers (pull, update,
# restart, backup and its download); admin: everything (configuration, merge)
ROLES = ("user", "developer", "admin")

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


TOKEN_ALGORITHM = "sha256"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """A token is random and long, so one SHA-256 is enough (no PBKDF2: MCP
    clients send it with every request)."""
    return f"{TOKEN_ALGORITHM}${hashlib.sha256(token.encode()).hexdigest()}"


@dataclass(frozen=True)
class Principal:
    """Who makes a request: a user (Basic), an MCP token or a linked instance."""

    name: str
    role: str
    kind: str = "user"
    # a linked instance: its user who acts through the link (a label only)
    on_behalf: str = ""

    def can(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)

    def __str__(self) -> str:
        """The name the jobs record as who started them."""
        if self.kind == "token":
            return f"token {self.name}"
        if self.kind == "link":
            return f"link {self.name}" + (f" ({self.on_behalf})" if self.on_behalf else "")
        return self.name


class Authenticator:
    """Check Basic credentials against the configured users.

    The browser sends the credentials with every request (pages, job polling,
    log stream), so a successful PBKDF2 check is remembered for the life of
    the process, keyed by a keyed hash of the password, never the password.
    """

    def __init__(self, users: dict[str, str], tokens: dict[str, str] | None = None,
                 links: dict[str, str] | None = None):
        self.users = users
        self.tokens = tokens or {}
        self.links = links or {}
        self._key = secrets.token_bytes(32)
        self._verified: dict[str, bytes] = {}
        self._dummy = hash_password(secrets.token_hex(8))

    def update(self, users: dict[str, str], tokens: dict[str, str], links: dict[str, str]) -> None:
        """New credentials: the passwords verified so far are checked again,
        so a changed or deleted one stops working at once."""
        self.users, self.tokens, self.links = users, tokens, links
        self._verified = {}

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

    def token_name(self, token: str) -> str | None:
        """The name of a configured MCP token, None when it is not one."""
        return _match(token, self.tokens)

    def link_name(self, token: str) -> str | None:
        """The name of a configured link token, None when it is not one."""
        return _match(token, self.links)


def _match(token: str, hashes: dict[str, str]) -> str | None:
    encoded = hash_token(token)
    found = None
    for name, expected in hashes.items():
        # every token compared, so the time does not tell which one matched
        if secrets.compare_digest(encoded, expected):
            found = name
    return found
