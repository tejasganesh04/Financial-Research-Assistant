"""Password hashing, policy and generation."""

import pytest

from src.services import passwords


def test_hash_and_verify():
    stored = passwords.hash_password("correct-horse-battery")
    assert stored.startswith(f"scrypt${passwords.N}${passwords.R}${passwords.P}$")
    assert passwords.verify_password("correct-horse-battery", stored)
    assert not passwords.verify_password("correct-horse-batterY", stored)


def test_same_password_gets_a_different_salt():
    assert passwords.hash_password("same-password-twice") != passwords.hash_password("same-password-twice")


@pytest.mark.parametrize(
    "stored", ["", "plain-text", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$AAAA$AAAA", "scrypt$16384$8$1$!!$!!"]
)
def test_malformed_hashes_never_match(stored):
    assert not passwords.verify_password("anything", stored)


def test_over_long_passwords_are_refused_without_hashing():
    stored = passwords.hash_password("x" * passwords.MAX_LENGTH)
    assert not passwords.verify_password("x" * (passwords.MAX_LENGTH + 1), stored)


def test_rehash_needed_for_other_parameters():
    assert not passwords.needs_rehash(passwords.hash_password("correct-horse-battery"))
    assert passwords.needs_rehash("scrypt$16384$8$1$AAAA$AAAA")


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short", "at least"),
        ("a" * 200, "at most"),
        (" leading-space-pass", "spaces"),
        ("aaaaaaaaaaaaaaaa", "repetitive"),
        ("a-perfectly-fine-passphrase", None),
    ],
)
def test_password_policy(password, problem):
    result = passwords.password_problem(password)
    assert (result is None) if problem is None else (problem in result)


def test_generated_passwords_are_readable_strong_and_unique():
    generated = {passwords.generate_password() for _ in range(50)}
    assert len(generated) == 50
    for password in generated:
        assert len(password) == 19 and password.count("-") == 3
        assert not set(password) & set("0O1lI")
        assert passwords.password_problem(password) is None
