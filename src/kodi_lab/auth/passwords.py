"""Argon2id password hashing with bounded password input."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

MAX_PASSWORD_LENGTH = 1024


class InvalidPasswordError(ValueError):
    """A proposed password is not acceptable input to the password KDF."""


class PasswordService:
    """Hash and verify passwords exclusively with Argon2id."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher(type=Type.ID)
        self._dummy_hash = self._hasher.hash("kodi-lab-dummy-password")

    def hash(self, password: str) -> str:
        self._validate(password)
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str | None) -> bool:
        if not password or len(password) > MAX_PASSWORD_LENGTH:
            return False
        candidate = self._dummy_hash if password_hash is None else password_hash
        try:
            valid = self._hasher.verify(candidate, password)
        except (VerificationError, InvalidHashError):
            return False
        return bool(valid and password_hash is not None)

    @staticmethod
    def _validate(password: str) -> None:
        if not password:
            raise InvalidPasswordError("password must not be empty")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise InvalidPasswordError("password is too long")
