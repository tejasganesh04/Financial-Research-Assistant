"""Password hashing with scrypt (standard library), and readable generated passwords.

Stored format: ``scrypt$<n>$<r>$<p>$<salt>$<hash>`` with base64 salt and hash, so the cost can be
raised later: ``needs_rehash`` reports hashes made with older parameters, and sign-in upgrades them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# About 150 ms and 32 MB per hash: slow for guessing, fine for a handful of sign-ins.
N, R, P = 2**15, 8, 3  # OWASP minimum for 32 MB of memory
SALT_BYTES = 16
KEY_BYTES = 32
MAX_MEMORY = 64 * 1024 * 1024

MIN_LENGTH = 12
MAX_LENGTH = 128  # bounds the hashing work a single request can cause

# No look-alike characters (0/O, 1/l/I), so passwords can be read out or copied by hand.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=MAX_MEMORY, dklen=KEY_BYTES)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_derive(password, salt, N, R, P))}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; malformed hashes and over-long inputs never match."""
    if len(password) > MAX_LENGTH:
        return False
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    return not stored.startswith(f"scrypt${N}${R}${P}$")


# Compared against when a username does not exist, so unknown and known names take the same time.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(password: str, username: str | None = None) -> str | None:
    """Why a new password is not acceptable, or None."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    if password.strip() != password:
        return "Remove spaces from the start and end."
    if len(set(password)) < 5:
        return "Use a less repetitive password."
    if username and len(username) >= 3 and username.lower() in password.lower():
        return "Don't include the username in the password."
    return None


def generate_password(groups: int = 4, size: int = 4) -> str:
    """A random password such as ``k7Pq-Xm3a-RtZ9-hWe4`` (about 92 bits)."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(size)) for _ in range(groups))
