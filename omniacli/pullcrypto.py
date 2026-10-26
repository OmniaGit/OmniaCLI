# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a puller carries, sealed: the messages and the backup files that two
OmniaCLI exchange through a puller, the relay of a third one that neither
reads nor writes them.

A puller may have a key: 32 random bytes that only the two ends hold (the
producer, whose instance the puller serves, and the consumer). With a key
every message is AES-256-GCM encrypted and authenticated: the relay cannot
read one, nor make one up, nor turn one of a puller or of a direction into
another (the puller's name and the direction are its associated data). Each
direction and the files have their own subkey (HKDF-SHA256 of the key), so a
message cannot be sent back the way it came. An end that has a key refuses a
message in clear: a relay that strips the encryption is not believed.
Without a key the messages travel in clear and the relay is trusted.

A command (consumer to producer) carries a random id and the time it was
made; ReplayGuard refuses one that is too old, from the future, or already
seen, so a command the relay kept cannot be played again.

A backup file is encrypted in chunks (the STREAM construction): every chunk
has the counter and a last-chunk flag in its nonce, under a key of its own
for every file, so a file cut short, lengthened or reordered does not
decrypt. A large dump is never held in memory.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# the text form of a key: this prefix and the 32 bytes in base64url
KEY_PREFIX = "opk1-"
KEY_BYTES = 32
FORMAT = 1
# the directions of a message
COMMAND = "command"  # consumer to producer
REPORT = "report"  # producer to consumer
DIRECTIONS = (COMMAND, REPORT)
# a command must reach the producer within WINDOW seconds of being made,
# give or take TOLERANCE seconds between the two clocks
WINDOW = 600
TOLERANCE = 120
# the actions a command may name: lower case letters, digits, _ and -
ACTION = re.compile(r"[a-z][a-z0-9_-]{0,31}")
COMMAND_ID = re.compile(r"[0-9a-f]{32}")

FILE_MAGIC = b"OCPF\x01"
FILE_CHUNK = 1 << 20
_SALT = 16
_TAG = 16
# magic, chunk size, salt
_HEADER = struct.Struct(f">{len(FILE_MAGIC)}sI{_SALT}s")


class PullerCryptoError(Exception):
    """A key, a message or a file that cannot be used; the message says why."""


class CommandRefused(Exception):
    """A command the producer does not run (too old, replayed...)."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    if not isinstance(text, str):
        raise PullerCryptoError("not base64 text")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PullerCryptoError("not base64 text") from exc


def new_key() -> str:
    """A new random key, in its text form."""
    return KEY_PREFIX + _b64(secrets.token_bytes(KEY_BYTES))


def parse_key(text: str) -> bytes:
    """The 32 bytes of a key in its text form."""
    text = (text or "").strip()
    if not text.startswith(KEY_PREFIX):
        raise PullerCryptoError(f"a puller key starts with {KEY_PREFIX}")
    raw = _unb64(text[len(KEY_PREFIX):])
    if len(raw) != KEY_BYTES:
        raise PullerCryptoError(f"a puller key holds {KEY_BYTES} bytes, this one {len(raw)}")
    return raw


def _subkey(key: bytes, purpose: str, salt: Optional[bytes] = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt,
                info=f"omniacli puller v{FORMAT} {purpose}".encode()).derive(key)


def _associated(puller: str, direction: str) -> bytes:
    return f"omniacli puller v{FORMAT}\0{puller}\0{direction}".encode()


class Sealer:
    """Seals and opens the messages of one puller, with its key or in clear
    (key None)."""

    def __init__(self, key: Optional[bytes], puller: str):
        if key is not None and len(key) != KEY_BYTES:
            raise PullerCryptoError(f"a puller key holds {KEY_BYTES} bytes")
        self.key = key
        self.puller = puller
        self._aead = {d: AESGCM(_subkey(key, d)) for d in DIRECTIONS} if key else {}

    @property
    def encrypted(self) -> bool:
        return self.key is not None

    def seal(self, direction: str, payload: dict) -> dict:
        """The envelope of payload (a JSON object), to send in direction."""
        if direction not in DIRECTIONS:
            raise ValueError(f"direction {direction!r}")
        if not self.key:
            return {"v": FORMAT, "enc": None, "p": payload}
        nonce = secrets.token_bytes(12)
        data = json.dumps(payload, separators=(",", ":")).encode()
        sealed = self._aead[direction].encrypt(nonce, data, _associated(self.puller, direction))
        return {"v": FORMAT, "enc": "aes-256-gcm", "n": _b64(nonce), "c": _b64(sealed)}

    def open(self, direction: str, envelope) -> dict:
        """The payload of an envelope received from direction."""
        if direction not in DIRECTIONS:
            raise ValueError(f"direction {direction!r}")
        if not isinstance(envelope, dict) or envelope.get("v") != FORMAT:
            raise PullerCryptoError(f"not a message of format {FORMAT}")
        enc = envelope.get("enc")
        if enc is None:
            if self.key:
                # with a key, a message in clear is the relay's, not the other end's
                raise PullerCryptoError("a message in clear on a puller with a key: refused")
            payload = envelope.get("p")
        elif enc != "aes-256-gcm":
            raise PullerCryptoError(f"unknown encryption {enc!r}")
        elif not self.key:
            raise PullerCryptoError("an encrypted message, and no key for this puller here")
        else:
            nonce, sealed = _unb64(envelope.get("n")), _unb64(envelope.get("c"))
            if len(nonce) != 12:
                raise PullerCryptoError("bad nonce")
            try:
                data = self._aead[direction].decrypt(nonce, sealed, _associated(self.puller, direction))
            except InvalidTag as exc:
                raise PullerCryptoError("the message does not open with this puller's key: another "
                                        "key, another puller, or changed on the way") from exc
            try:
                payload = json.loads(data)
            except ValueError as exc:
                raise PullerCryptoError("the message is not JSON") from exc
        if not isinstance(payload, dict):
            raise PullerCryptoError("the message is not a JSON object")
        return payload


# --- commands -------------------------------------------------------------------


@dataclass
class Command:
    """What a consumer asks the producer: action on the puller's instance,
    with its params, by requester (who, on the consumer's side)."""

    action: str
    requester: str
    params: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "Command":
        if not isinstance(data, dict):
            raise PullerCryptoError("a command is a JSON object")
        action, requester = data.get("action"), data.get("requester")
        params, cid, created = data.get("params", {}), data.get("id"), data.get("created")
        if not isinstance(action, str) or not ACTION.fullmatch(action):
            raise PullerCryptoError(f"bad action {action!r}")
        if not isinstance(requester, str) or not 0 < len(requester) <= 200:
            raise PullerCryptoError("bad requester")
        if not isinstance(params, dict):
            raise PullerCryptoError("the params of a command are a JSON object")
        if not isinstance(cid, str) or not COMMAND_ID.fullmatch(cid):
            raise PullerCryptoError("bad command id")
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            raise PullerCryptoError("bad command time")
        return cls(action=action, requester=requester, params=params, id=cid, created=float(created))


class ReplayGuard:
    """The producer's memory of the commands it took: one is refused when it
    was made more than window seconds ago, or more than tolerance seconds in
    the future (the two clocks differ), or when its id was already seen. An
    id is remembered as long as its command could still be accepted."""

    def __init__(self, window: float = WINDOW, tolerance: float = TOLERANCE,
                 seen: Optional[dict] = None):
        self.window = window
        self.tolerance = tolerance
        # {id: time after which its command is refused anyway}
        self.seen: dict[str, float] = dict(seen or {})

    def check(self, command: Command, now: Optional[float] = None) -> None:
        """Accept command (and remember it) or raise CommandRefused."""
        now = time.time() if now is None else now
        self.prune(now)
        age = now - command.created
        if age < -self.tolerance:
            raise CommandRefused(f"made {-age:.0f} s in the future: the clocks of the two ends differ "
                                 f"by more than {self.tolerance:.0f} s")
        if age > self.window + self.tolerance:
            raise CommandRefused(f"made {age:.0f} s ago, more than {self.window:.0f} s: too old")
        if command.id in self.seen:
            raise CommandRefused("already received: a command runs once")
        self.seen[command.id] = command.created + self.window + self.tolerance

    def prune(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.seen = {cid: until for cid, until in self.seen.items() if until >= now}

    def state(self) -> dict:
        """What to save, so that a restart does not forget the ids."""
        return dict(self.seen)


# --- files ----------------------------------------------------------------------

Progress = Optional[Callable[[int, int], None]]


def is_encrypted_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(FILE_MAGIC)) == FILE_MAGIC
    except OSError:
        return False


def _chunk_nonce(counter: int, last: bool) -> bytes:
    if counter >= 1 << 64:
        raise PullerCryptoError("file too large")
    return struct.pack(">Q3xB", counter, 1 if last else 0)


def _file_aead(key: bytes, puller: str, salt: bytes) -> AESGCM:
    return AESGCM(_subkey(_subkey(key, f"file {puller}"), "chunks", salt))


def encrypt_file(key: bytes, puller: str, src: Path, dst: Path, progress: Progress = None,
                 chunk: int = FILE_CHUNK) -> None:
    """src encrypted into dst (replaced only once complete)."""
    salt = secrets.token_bytes(_SALT)
    header = _HEADER.pack(FILE_MAGIC, chunk, salt)
    aead = _file_aead(key, puller, salt)
    total = src.stat().st_size
    partial = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as fin, open(partial, "wb") as fout:
            fout.write(header)
            done, counter = 0, 0
            block = fin.read(chunk)
            while True:
                after = fin.read(chunk)
                last = not after
                fout.write(aead.encrypt(_chunk_nonce(counter, last), block, header))
                done += len(block)
                if progress:
                    progress(done, total)
                if last:
                    break
                block, counter = after, counter + 1
        os.replace(partial, dst)
    finally:
        partial.unlink(missing_ok=True)


def decrypt_file(key: bytes, puller: str, src: Path, dst: Path, progress: Progress = None) -> None:
    """src decrypted into dst (written only when every chunk opened, the last
    one included); PullerCryptoError otherwise, and no dst."""
    total = src.stat().st_size
    partial = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as fin, open(partial, "wb") as fout:
            header = fin.read(_HEADER.size)
            if len(header) != _HEADER.size:
                raise PullerCryptoError(f"{src.name} is not an encrypted backup")
            magic, chunk, salt = _HEADER.unpack(header)
            if magic != FILE_MAGIC:
                raise PullerCryptoError(f"{src.name} is not an encrypted backup")
            if not 0 < chunk <= 64 << 20:
                raise PullerCryptoError(f"{src.name}: bad chunk size")
            aead = _file_aead(key, puller, salt)
            size, counter, done = chunk + _TAG, 0, len(header)
            block = fin.read(size)
            while True:
                after = fin.read(size)
                last = not after
                try:
                    fout.write(aead.decrypt(_chunk_nonce(counter, last), block, header))
                except InvalidTag as exc:
                    raise PullerCryptoError(
                        f"{src.name} does not decrypt with this puller's key: another key, another "
                        "puller, or the file is damaged or cut short") from exc
                done += len(block)
                if progress:
                    progress(done, total)
                if last:
                    break
                block, counter = after, counter + 1
        os.replace(partial, dst)
    finally:
        partial.unlink(missing_ok=True)
