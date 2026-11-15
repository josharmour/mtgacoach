"""Enroll two currently witnessed Arena personas for a developer challenge.

Each machine supplies an owner-local keyed roster observation. The resulting
profile contains only the two selected account fingerprints and their key.
"""

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class IdentityUnavailable(ValueError):
    """A bounded local error that contains no endpoint or identifying value."""


def _token(value):
    if not isinstance(value, str):
        raise IdentityUnavailable("Invalid native pairing evidence")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise IdentityUnavailable("Invalid native pairing evidence") from None
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise IdentityUnavailable("Invalid native pairing evidence")
    return value


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise IdentityUnavailable("Duplicate fields in native pairing evidence")
    return result


def _json_file(path, limit):
    with open(path, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise IdentityUnavailable("A local regular file is required")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise IdentityUnavailable("Invalid native pairing evidence")
    try:
        value = json.loads(data, object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError):
        raise IdentityUnavailable("Invalid native pairing evidence") from None
    if not isinstance(value, dict):
        raise IdentityUnavailable("Invalid native pairing evidence")
    return value


def _roster(row):
    friends = row.get("friends")
    if (
        row.get("social_connected") is not True
        or row.get("social_enabled") is not True
        or type(friends) is not list
        or type(row.get("num_friends")) is not int
        or type(row.get("get_count")) is not int
        or not 0 < len(friends) == row["num_friends"] == row["get_count"] <= 256
    ):
        raise IdentityUnavailable("Both native friend lists must be connected and complete")
    own = _token(row.get("own"))
    peers = {_token(v) for v in friends}
    if len(peers) != len(friends) or own in peers:
        raise IdentityUnavailable("Arena returned ambiguous native account evidence")
    return own, peers


@dataclass(frozen=True, repr=False)
class EnrolledPair:
    key: bytes
    issuer: str
    acceptor: str

    def token(self, name, persona):
        if not all(isinstance(t, str) and t and "\0" not in t for t in (name, persona)):
            return None
        result = hmac.digest(self.key, (name + "\0" + persona).encode("utf-8"), hashlib.sha256)
        return base64.b64encode(result).decode("ascii")

    def matches(self, role, name, persona):
        actual = self.token(name, persona)
        expected = self.issuer if role == "issuer" else self.acceptor
        return role in ("issuer", "acceptor") and actual is not None and hmac.compare_digest(actual, expected)


def enrollment(key, issuer_receipt, acceptor_receipt):
    if not isinstance(key, bytes) or len(key) != 32:
        raise IdentityUnavailable("A 32-byte local pairing key is required")
    issuer, issuer_friends = _roster(issuer_receipt)
    acceptor, acceptor_friends = _roster(acceptor_receipt)
    if issuer == acceptor or acceptor not in issuer_friends or issuer not in acceptor_friends:
        raise IdentityUnavailable("The two observed Arena accounts do not uniquely attest each other")
    return {
        "version": 1,
        "key": base64.b64encode(key).decode("ascii"),
        "issuer": issuer,
        "acceptor": acceptor,
    }


def load_enrollment(path):
    raw = _json_file(path, 512)
    if (
        set(raw) != {"version", "key", "issuer", "acceptor"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
    ):
        raise IdentityUnavailable("Invalid local pairing profile")
    key = base64.b64decode(_token(raw["key"]))
    issuer, acceptor = _token(raw["issuer"]), _token(raw["acceptor"])
    if issuer == acceptor:
        raise IdentityUnavailable("The local pairing profile does not identify two distinct accounts")
    return EnrolledPair(key, issuer, acceptor)


def write_private_new(path, data):
    """Publish a complete owner-local receipt, without replacing another writer."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    from arenamcp.dev_challenge_pair import source_enabled

    parser = argparse.ArgumentParser(
        description="Prepare one owner-local developer pair from two native roster receipts."
    )
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--issuer-receipt", required=True, type=Path)
    parser.add_argument("--acceptor-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not source_enabled():
        parser.exit(2, "Enable direct challenges in a developer source checkout first.\n")
    try:
        with args.key_file.open("rb") as key_stream:
            key = key_stream.read(33)
        profile = enrollment(
            key, _json_file(args.issuer_receipt, 32768), _json_file(args.acceptor_receipt, 32768)
        )
        encoded = json.dumps(profile, separators=(",", ":")).encode("ascii")
        # Exclusive creation keeps an existing role binding or differently
        # provisioned key intact. Only the selected pair enters this file.
        write_private_new(args.output, encoded)
    except (OSError, IdentityUnavailable):
        parser.exit(2, "Could not enroll this pair from its local evidence. Use a new private output file.\n")
    print("Created the local developer pairing profile.")


if __name__ == "__main__":
    main()
