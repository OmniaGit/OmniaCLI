# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signature of a published file: a Debian package (packages.py), an
installer, anything uploaded for download.

Next to <file> the publisher puts:

- <file>.json, the manifest: {"file", "version", "sha256", "size", "built"},
  and "package" for a .deb; what is published, readable without downloading
  the file;
- <file>.json.sig, the Ed25519 signature of the exact bytes of the
  manifest, in base64;
- <file>.sha256, the line sha256sum writes, for a check by hand (not
  signed: the manifest holds the same checksum, signed).

The private key stays on the machine that builds the files, encrypted with
a passphrase (omniacli package-key); whoever checks a file knows only the
public key, "ed25519:<base64 of its 32 bytes>". Whoever controls the
download server can then withhold a file, not make one up nor change its
version: a manifest it writes does not verify. One key signs every file, so
a manifest is believed only for the file it names: the signed manifest of
another file cannot stand in for this one's.

The format is meant to be made by any Ed25519 implementation (the release
scripts make it without OmniaCLI); sign_file is what omniacli package-sign
does.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_PREFIX = "ed25519:"
MANIFEST_SUFFIX = ".json"
SIGNATURE_SUFFIX = ".json.sig"
CHECKSUM_SUFFIX = ".sha256"
# a manifest or signature file larger than this is not one
MAX_SMALL_FILE = 16384
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SigningError(ValueError):
    pass


def parse_public_key(text: str) -> Ed25519PublicKey:
    text = (text or "").strip()
    if not text.startswith(KEY_PREFIX):
        raise SigningError(f"a public key starts with {KEY_PREFIX} (omniacli package-key prints it)")
    try:
        raw = base64.b64decode(text[len(KEY_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        raise SigningError("the public key is not valid base64") from None
    if len(raw) != 32:
        raise SigningError(f"an Ed25519 public key has 32 bytes, this one {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def public_key_text(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return KEY_PREFIX + base64.b64encode(raw).decode()


def new_private_key(passphrase: bytes) -> tuple[bytes, str]:
    """A new key: its private half as PKCS#8 PEM encrypted with passphrase,
    and its public key text."""
    if not passphrase:
        raise SigningError("the private key needs a passphrase")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(passphrase))
    return pem, public_key_text(key.public_key())


def load_private_key(pem: bytes, passphrase: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=passphrase or None)
    except (ValueError, TypeError) as exc:
        raise SigningError(f"cannot read the private key: {exc}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("the private key is not an Ed25519 key")
    return key


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_of(path: Path, version: str, package: str | None = None) -> bytes:
    """The manifest of the file at path, as it is signed and published."""
    if not version:
        raise SigningError(f"{path.name}: no version for its manifest")
    data = {"file": path.name, "version": version, "sha256": file_sha256(path),
            "size": path.stat().st_size,
            "built": datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}
    if package:
        data["package"] = package
    return (json.dumps(data, indent=2) + "\n").encode()


def sign_bytes(key: Ed25519PrivateKey, data: bytes) -> bytes:
    return base64.b64encode(key.sign(data)) + b"\n"


def sign_file(key: Ed25519PrivateKey, path: Path, version: str, package: str | None = None) -> list[Path]:
    """Write path.json, path.json.sig and path.sha256 next to path."""
    manifest = manifest_of(path, version, package)
    sha256 = json.loads(manifest)["sha256"]
    written = []
    for suffix, content in ((CHECKSUM_SUFFIX, f"{sha256}  {path.name}\n".encode()),
                            (MANIFEST_SUFFIX, manifest), (SIGNATURE_SUFFIX, sign_bytes(key, manifest))):
        target = path.with_name(path.name + suffix)
        target.write_bytes(content)
        written.append(target)
    return written


def verified_manifest(public_key: str, manifest: bytes, signature: bytes, file_name: str) -> dict:
    """The manifest of file_name whose signature verifies with public_key;
    SigningError otherwise, or when it is the manifest of another file."""
    key = parse_public_key(public_key)
    try:
        raw = base64.b64decode(signature.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SigningError("the signature is not valid base64") from None
    try:
        key.verify(raw, manifest)
    except InvalidSignature:
        raise SigningError("the signature of the manifest does not verify with the public key: "
                           "not signed with its private key, or changed after signing") from None
    try:
        data = json.loads(manifest)
    except ValueError:
        raise SigningError("the signed manifest is not JSON") from None
    if not isinstance(data, dict):
        raise SigningError("the signed manifest is not a JSON object")
    if data.get("file") != file_name:
        raise SigningError(f"the signed manifest is of {data.get('file')!r}, not {file_name!r}")
    if not isinstance(data.get("version"), str) or not data["version"]:
        raise SigningError("the signed manifest has no version")
    if not isinstance(data.get("sha256"), str) or not _SHA256.fullmatch(data["sha256"]):
        raise SigningError("the signed manifest has no sha256")
    if not isinstance(data.get("size"), int) or isinstance(data["size"], bool) or data["size"] < 0:
        raise SigningError("the signed manifest has no size")
    return data


def verify_local(public_key: str, path: Path) -> dict:
    """A downloaded file checked against the signed manifest next to it."""
    try:
        manifest = path.with_name(path.name + MANIFEST_SUFFIX).read_bytes()
        signature = path.with_name(path.name + SIGNATURE_SUFFIX).read_bytes()
    except OSError as exc:
        raise SigningError(f"{exc.filename}: {exc.strerror}") from None
    data = verified_manifest(public_key, manifest, signature, path.name)
    if path.stat().st_size != data["size"] or file_sha256(path) != data["sha256"]:
        raise SigningError(f"{path.name} is not the file the manifest signs (sha256 or size differ)")
    return data
