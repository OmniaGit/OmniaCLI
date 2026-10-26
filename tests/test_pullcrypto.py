# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sealed messages, the commands and the encrypted files of a puller."""
import os

import pytest

from omniacli import pullcrypto
from omniacli.pullcrypto import (
    COMMAND,
    REPORT,
    Command,
    CommandRefused,
    PullerCryptoError,
    ReplayGuard,
    Sealer,
    decrypt_file,
    encrypt_file,
    is_encrypted_file,
    new_key,
    parse_key,
)

KEY = parse_key(new_key())


# --- keys -------------------------------------------------------------------

def test_a_key_round_trips_and_differs_every_time():
    text = new_key()
    assert text.startswith("opk1-") and len(parse_key(text)) == 32
    assert parse_key(f"  {text}\n") == parse_key(text)
    assert new_key() != text


@pytest.mark.parametrize("text", ["", "abc", "opk1-", "opk1-!!!", "opk2-" + new_key()[5:],
                                  "opk1-" + "A" * 20])
def test_a_bad_key_is_refused(text):
    with pytest.raises(PullerCryptoError):
        parse_key(text)


# --- messages ---------------------------------------------------------------

def test_sealed_message_round_trip_and_not_readable():
    sealer = Sealer(KEY, "api-prod")
    env = sealer.seal(COMMAND, {"action": "backup", "secret": "s3cr3t"})
    assert env["enc"] == "aes-256-gcm" and "s3cr3t" not in str(env) and "backup" not in str(env)
    assert Sealer(KEY, "api-prod").open(COMMAND, env) == {"action": "backup", "secret": "s3cr3t"}
    # a new nonce every time
    assert sealer.seal(COMMAND, {"a": 1})["n"] != sealer.seal(COMMAND, {"a": 1})["n"]


def test_clear_message_round_trip():
    sealer = Sealer(None, "api-prod")
    env = sealer.seal(REPORT, {"state": "active"})
    assert env == {"v": 1, "enc": None, "p": {"state": "active"}}
    assert sealer.open(REPORT, env) == {"state": "active"}


def test_a_message_opens_only_with_its_key_puller_and_direction():
    env = Sealer(KEY, "api-prod").seal(COMMAND, {"action": "stop"})
    for sealer, direction in ((Sealer(parse_key(new_key()), "api-prod"), COMMAND),
                              (Sealer(KEY, "other"), COMMAND),
                              # sent back the way it came: refused
                              (Sealer(KEY, "api-prod"), REPORT)):
        with pytest.raises(PullerCryptoError):
            sealer.open(direction, env)


def test_a_changed_message_is_refused():
    env = Sealer(KEY, "api-prod").seal(COMMAND, {"action": "backup"})
    raw = bytearray(pullcrypto._unb64(env["c"]))
    raw[0] ^= 1
    with pytest.raises(PullerCryptoError, match="changed on the way"):
        Sealer(KEY, "api-prod").open(COMMAND, {**env, "c": pullcrypto._b64(bytes(raw))})


def test_with_a_key_a_message_in_clear_is_refused():
    # a relay that strips the encryption is not believed
    clear = Sealer(None, "api-prod").seal(COMMAND, {"action": "stop"})
    with pytest.raises(PullerCryptoError, match="in clear"):
        Sealer(KEY, "api-prod").open(COMMAND, clear)


def test_without_a_key_an_encrypted_message_is_refused():
    env = Sealer(KEY, "api-prod").seal(COMMAND, {"action": "stop"})
    with pytest.raises(PullerCryptoError, match="no key"):
        Sealer(None, "api-prod").open(COMMAND, env)


@pytest.mark.parametrize("env", [None, [], {}, {"v": 2, "enc": None, "p": {}}, {"v": 1, "enc": "rot13"},
                                 {"v": 1, "enc": None, "p": [1]}, {"v": 1, "enc": None}])
def test_malformed_envelopes_are_refused(env):
    with pytest.raises(PullerCryptoError):
        Sealer(None, "p").open(COMMAND, env)


def test_malformed_encrypted_envelopes_are_refused():
    sealer = Sealer(KEY, "p")
    good = sealer.seal(COMMAND, {"a": 1})
    for env in ({**good, "n": "AAAA"}, {**good, "c": "!!"}, {**good, "n": None}):
        with pytest.raises(PullerCryptoError):
            sealer.open(COMMAND, env)


# --- commands ---------------------------------------------------------------

def test_a_command_round_trips_through_a_sealed_message():
    cmd = Command("backup", "dev: mboscolo", {"no_filestore": True})
    sealer = Sealer(KEY, "api-prod")
    back = Command.from_dict(sealer.open(COMMAND, sealer.seal(COMMAND, cmd.to_dict())))
    assert back == cmd and len(cmd.id) == 32


@pytest.mark.parametrize("change", [{"action": "Stop"}, {"action": "x;rm"}, {"requester": ""},
                                    {"params": []}, {"id": "abc"}, {"created": "now"},
                                    {"created": True}])
def test_a_bad_command_is_refused(change):
    with pytest.raises(PullerCryptoError):
        Command.from_dict({**Command("backup", "dev").to_dict(), **change})


def test_replay_guard():
    guard = ReplayGuard(window=600, tolerance=120)
    now = 1_000_000.0
    fresh = Command("backup", "dev", created=now - 30)
    guard.check(fresh, now)
    with pytest.raises(CommandRefused, match="already received"):
        guard.check(fresh, now + 1)
    # old: past window + tolerance
    with pytest.raises(CommandRefused, match="too old"):
        guard.check(Command("backup", "dev", created=now - 721), now)
    guard.check(Command("backup", "dev", created=now - 719), now)
    # from the future: past the tolerance
    with pytest.raises(CommandRefused, match="future"):
        guard.check(Command("backup", "dev", created=now + 121), now)
    guard.check(Command("backup", "dev", created=now + 119), now)


def test_replay_guard_forgets_only_what_cannot_come_back():
    now = 1_000_000.0
    guard = ReplayGuard(window=600, tolerance=120)
    cmd = Command("stop", "dev", created=now)
    guard.check(cmd, now)
    # saved and loaded again (a restart of the producer)
    again = ReplayGuard(window=600, tolerance=120, seen=guard.state())
    with pytest.raises(CommandRefused, match="already received"):
        again.check(cmd, now + 700)
    again.prune(now + 721)
    assert again.seen == {}
    # and once forgotten, it is too old anyway
    with pytest.raises(CommandRefused, match="too old"):
        again.check(cmd, now + 721)


# --- files ------------------------------------------------------------------

CHUNK = 64


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 5 * CHUNK + 7])
def test_a_file_round_trips(tmp_path, size):
    src, enc, out = tmp_path / "a.zip", tmp_path / "a.zip.enc", tmp_path / "b.zip"
    data = os.urandom(size)
    src.write_bytes(data)
    seen = []
    encrypt_file(KEY, "api-prod", src, enc, progress=lambda done, total: seen.append((done, total)), chunk=CHUNK)
    assert is_encrypted_file(enc) and not is_encrypted_file(src)
    assert seen[-1] == (size, size)
    if size >= 16:
        assert data[:16] not in enc.read_bytes()
    decrypt_file(KEY, "api-prod", enc, out)
    assert out.read_bytes() == data
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.zip", "a.zip.enc", "b.zip"]


def test_the_same_file_encrypts_differently_every_time(tmp_path):
    src = tmp_path / "a"
    src.write_bytes(b"x" * 200)
    encrypt_file(KEY, "p", src, tmp_path / "1", chunk=CHUNK)
    encrypt_file(KEY, "p", src, tmp_path / "2", chunk=CHUNK)
    assert (tmp_path / "1").read_bytes() != (tmp_path / "2").read_bytes()


def _encrypted(tmp_path, size=5 * CHUNK + 7):
    src, enc = tmp_path / "a", tmp_path / "a.enc"
    src.write_bytes(os.urandom(size))
    encrypt_file(KEY, "api-prod", src, enc, chunk=CHUNK)
    return enc


def _refused(tmp_path, enc, key=KEY, puller="api-prod"):
    out = tmp_path / "out"
    with pytest.raises(PullerCryptoError):
        decrypt_file(key, puller, enc, out)
    # nothing half written is left
    assert not out.exists() and not (tmp_path / "out.part").exists()


def test_a_file_opens_only_with_its_key_and_puller(tmp_path):
    enc = _encrypted(tmp_path)
    _refused(tmp_path, enc, key=parse_key(new_key()))
    _refused(tmp_path, enc, puller="other")


def test_a_file_cut_at_a_chunk_boundary_is_refused(tmp_path):
    enc = _encrypted(tmp_path)
    data = enc.read_bytes()
    header = pullcrypto._HEADER.size
    # the header and the first two chunks only: each opened alone, but not as the last
    enc.write_bytes(data[:header + 2 * (CHUNK + 16)])
    _refused(tmp_path, enc)


def test_a_file_cut_inside_a_chunk_is_refused(tmp_path):
    enc = _encrypted(tmp_path)
    enc.write_bytes(enc.read_bytes()[:-5])
    _refused(tmp_path, enc)


def test_a_file_lengthened_is_refused(tmp_path):
    enc = _encrypted(tmp_path)
    enc.write_bytes(enc.read_bytes() + b"\0" * (CHUNK + 16))
    _refused(tmp_path, enc)


def test_a_file_with_chunks_swapped_is_refused(tmp_path):
    enc = _encrypted(tmp_path)
    data = enc.read_bytes()
    h, s = pullcrypto._HEADER.size, CHUNK + 16
    enc.write_bytes(data[:h] + data[h + s:h + 2 * s] + data[h:h + s] + data[h + 2 * s:])
    _refused(tmp_path, enc)


def test_a_changed_byte_or_header_is_refused(tmp_path):
    enc = _encrypted(tmp_path)
    data = bytearray(enc.read_bytes())
    for where in (len(data) // 2, len(pullcrypto.FILE_MAGIC) + 6):  # a chunk, the salt
        changed = bytearray(data)
        changed[where] ^= 1
        enc.write_bytes(bytes(changed))
        _refused(tmp_path, enc)


def test_a_file_that_is_not_encrypted_is_refused(tmp_path):
    plain = tmp_path / "plain.zip"
    plain.write_bytes(b"PK\3\4" + b"\0" * 100)
    _refused(tmp_path, plain)
    plain.write_bytes(b"")
    _refused(tmp_path, plain)


def test_a_megabyte_file_with_the_default_chunk(tmp_path):
    src, enc, out = tmp_path / "a", tmp_path / "e", tmp_path / "o"
    data = os.urandom(3 * pullcrypto.FILE_CHUNK + 123)
    src.write_bytes(data)
    encrypt_file(KEY, "p", src, enc)
    assert enc.stat().st_size == len(data) + pullcrypto._HEADER.size + 4 * 16
    decrypt_file(KEY, "p", enc, out)
    assert out.read_bytes() == data
