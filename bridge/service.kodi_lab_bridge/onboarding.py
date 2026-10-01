"""Kodi-local, bounded OTP state. No network bootstrap is exposed here."""

import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import stat
import threading
import time
import uuid
from contextlib import contextmanager, suppress

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 4
CODE_TTL = 300
MAX_FAILURES = 5
SOURCE_WINDOW = 300
BRIDGE_WINDOW = 600
GENERATION_COOLDOWN = 30
MAX_STATE_BYTES = 8192
MAX_SOURCES = 64
MAX_BOOTSTRAP_REPLAY = 32
BOOTSTRAP_REPLAY_TTL = 600
PROVISIONAL_TTL = 300
BOOTSTRAP_VERSION = "otp-1"
_THREAD_LOCK = threading.RLock()


class OnboardingError(RuntimeError):
    """A bounded state or policy failure; never includes the OTP."""


def _valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _window(values, now, duration):
    return [value for value in values if now - duration < value <= now]


def _default():
    return {
        "format": 2,
        "code": None,
        "code_expired": False,
        "created_at": None,
        "expires_at": None,
        "failed_attempts": 0,
        "source_failures": {},
        "bridge_failures": [],
        "last_generated_at": None,
        "transaction": None,
        "bootstrap_replay": [],
        "committed_request_id": None,
    }


def _validate(value):
    if not isinstance(value, dict) or set(value) != set(_default()):
        raise OnboardingError("onboarding_state_invalid")
    if value["format"] != 2 or type(value["failed_attempts"]) is not int:
        raise OnboardingError("onboarding_state_invalid")
    if not 0 <= value["failed_attempts"] <= MAX_FAILURES:
        raise OnboardingError("onboarding_state_invalid")
    code = value["code"]
    if type(value["code_expired"]) is not bool:
        raise OnboardingError("onboarding_state_invalid")
    if code is not None and (
        not isinstance(code, str)
        or len(code) != CODE_LENGTH
        or any(character not in ALPHABET for character in code)
    ):
        raise OnboardingError("onboarding_state_invalid")
    created = value["created_at"]
    expires = value["expires_at"]
    if code is not None and (
        not _valid_time(created)
        or not _valid_time(expires)
        or expires != created + CODE_TTL
    ):
        raise OnboardingError("onboarding_state_invalid")
    if code is None and (
        created is not None or expires is not None or value["code_expired"]
    ):
        raise OnboardingError("onboarding_state_invalid")
    generated = value["last_generated_at"]
    if generated is not None and not _valid_time(generated):
        raise OnboardingError("onboarding_state_invalid")
    if code is not None and generated != created:
        raise OnboardingError("onboarding_state_invalid")
    bridge_failures = value["bridge_failures"]
    if (
        not isinstance(bridge_failures, list)
        or len(bridge_failures) > MAX_FAILURES
        or not all(_valid_time(item) for item in bridge_failures)
    ):
        raise OnboardingError("onboarding_state_invalid")
    sources = value["source_failures"]
    if not isinstance(sources, dict) or len(sources) > MAX_SOURCES:
        raise OnboardingError("onboarding_state_invalid")
    for source, failures in sources.items():
        try:
            if str(ipaddress.ip_address(source)) != source:
                raise ValueError
        except (TypeError, ValueError):
            raise OnboardingError("onboarding_state_invalid") from None
        if (
            not isinstance(failures, list)
            or len(failures) > MAX_FAILURES
            or not all(_valid_time(item) for item in failures)
        ):
            raise OnboardingError("onboarding_state_invalid")
    transaction = value["transaction"]
    if transaction is not None:
        legacy = {"target_id", "request_id", "expires_at"}
        provisional = legacy | {
            "bridge_id",
            "secret",
            "credential_digest",
            "bootstrap_version",
            "nonce",
            "replay",
        }
        if not isinstance(transaction, dict) or set(transaction) not in (
            legacy,
            provisional,
        ):
            raise OnboardingError("onboarding_state_invalid")
        try:
            uuid.UUID(transaction["target_id"])
            uuid.UUID(transaction["request_id"])
        except (TypeError, ValueError, AttributeError):
            raise OnboardingError("onboarding_state_invalid") from None
        if not _valid_time(transaction["expires_at"]) or code is not None:
            raise OnboardingError("onboarding_state_invalid")
        if set(transaction) == provisional:
            try:
                uuid.UUID(transaction["bridge_id"])
                secret = bytes.fromhex(transaction["secret"])
            except (TypeError, ValueError, AttributeError):
                raise OnboardingError("onboarding_state_invalid") from None
            if (
                len(secret) != 32
                or transaction["secret"] != secret.hex()
                or transaction["credential_digest"]
                != hashlib.sha256(secret).hexdigest()
                or transaction["bootstrap_version"] != BOOTSTRAP_VERSION
                or not isinstance(transaction["nonce"], str)
                or len(transaction["nonce"]) != 43
                or not isinstance(transaction["replay"], list)
                or len(transaction["replay"]) > MAX_BOOTSTRAP_REPLAY
            ):
                raise OnboardingError("onboarding_state_invalid")
            for item in transaction["replay"]:
                if (
                    not isinstance(item, list)
                    or len(item) != 3
                    or not _valid_time(item[2])
                ):
                    raise OnboardingError("onboarding_state_invalid")
                try:
                    uuid.UUID(item[0])
                except (TypeError, ValueError, AttributeError):
                    raise OnboardingError("onboarding_state_invalid") from None
                if not isinstance(item[1], str) or len(item[1]) != 43:
                    raise OnboardingError("onboarding_state_invalid")
    bootstrap_replay = value["bootstrap_replay"]
    if (
        not isinstance(bootstrap_replay, list)
        or len(bootstrap_replay) > MAX_BOOTSTRAP_REPLAY
    ):
        raise OnboardingError("onboarding_state_invalid")
    for item in bootstrap_replay:
        if not isinstance(item, list) or len(item) != 3 or not _valid_time(item[2]):
            raise OnboardingError("onboarding_state_invalid")
        try:
            uuid.UUID(item[0])
        except (TypeError, ValueError, AttributeError):
            raise OnboardingError("onboarding_state_invalid") from None
        if not isinstance(item[1], str) or len(item[1]) != 43:
            raise OnboardingError("onboarding_state_invalid")
    committed = value["committed_request_id"]
    if committed is not None:
        try:
            if str(uuid.UUID(committed)) != committed:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise OnboardingError("onboarding_state_invalid") from None
        if transaction is not None or code is not None:
            raise OnboardingError("onboarding_state_invalid")
    return value


@contextmanager
def _file_lock(path):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    with _THREAD_LOCK:
        try:
            existing = os.lstat(lock_path)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OnboardingError("onboarding_state_invalid")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OnboardingError("onboarding_state_invalid")
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class OnboardingStore:
    def __init__(self, path, *, clock=None, choose=None, event=None):
        self.path = os.fspath(path)
        self.clock = clock or time.time
        self.choose = choose or secrets.choice
        self.event = event or (lambda _name: None)

    def reset_pairing(self):
        """Discard OTP and provisional credentials after local user confirmation."""
        with _file_lock(self.path):
            self._write(_default())

    def _read(self):
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_STATE_BYTES
            or metadata.st_nlink != 1
        ):
            raise OnboardingError("onboarding_state_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > MAX_STATE_BYTES
                or opened.st_nlink != 1
            ):
                raise OnboardingError("onboarding_state_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_STATE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_STATE_BYTES:
            raise OnboardingError("onboarding_state_invalid")
        try:
            state = json.loads(raw)
            if (
                isinstance(state, dict)
                and state.get("format") == 1
                and set(state)
                == set(_default()) - {"bootstrap_replay", "committed_request_id"}
            ):
                state["format"] = 2
                state["bootstrap_replay"] = []
                state["committed_request_id"] = None
                self._write(state)
            return _validate(state)
        except (ValueError, UnicodeError, TypeError):
            raise OnboardingError("onboarding_state_invalid") from None

    def _write(self, state):
        _validate(state)
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(raw) > MAX_STATE_BYTES:
            raise OnboardingError("onboarding_state_invalid")
        temporary = self.path + "." + uuid.uuid4().hex + ".tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def _now(self):
        now = self.clock()
        if not _valid_time(now):
            raise OnboardingError("onboarding_clock_invalid")
        return now

    def _check_clock(self, state, now):
        generated = state["last_generated_at"]
        if generated is not None and now < generated:
            raise OnboardingError("onboarding_clock_invalid")

    def _prune(self, state, now):
        state["bridge_failures"] = _window(state["bridge_failures"], now, BRIDGE_WINDOW)
        state["source_failures"] = {
            source: current
            for source, failures in state["source_failures"].items()
            if (current := _window(failures, now, SOURCE_WINDOW))
        }
        state["bootstrap_replay"] = _window_replay(state["bootstrap_replay"], now)
        transaction = state["transaction"]
        if transaction is not None and now >= transaction["expires_at"]:
            state["transaction"] = None

    def _expire(self, state, now):
        if (
            state["code"] is not None
            and not state["code_expired"]
            and now >= state["expires_at"]
        ):
            state["code_expired"] = True
            self._write(state)
            self.event("code_expired")

    def _new_code(self, state, now):
        previous = state["code"]
        for _ in range(8):
            code = "".join(self.choose(ALPHABET) for _ in range(CODE_LENGTH))
            if len(code) != CODE_LENGTH or any(
                character not in ALPHABET for character in code
            ):
                raise OnboardingError("onboarding_random_invalid")
            if code != previous:
                break
        else:
            raise OnboardingError("onboarding_random_invalid")
        state.update(
            code=code,
            code_expired=False,
            created_at=now,
            expires_at=now + CODE_TTL,
            failed_attempts=0,
            last_generated_at=now,
            transaction=None,
            committed_request_id=None,
        )
        self._write(state)
        self.event("code_generated")
        return code

    def initialize(self, *, paired):
        if paired:
            return "Paired"
        with _file_lock(self.path):
            state = self._read()
            if state is None:
                self._new_code(_default(), self._now())
            state = self._read()
            now = self._now()
            self._check_clock(state, now)
            self._expire(state, now)
            return self._status(state, now)["status"]

    def _status(self, state, now):
        if state is None:
            return {"status": "Not paired", "code": None, "expires_at": None}
        code = state["code"]
        expires = state["expires_at"]
        if code is None:
            name = "Not paired"
        elif state["code_expired"] or now >= expires:
            name = "Pairing code expired"
        elif (
            state["failed_attempts"] >= MAX_FAILURES
            or len(_window(state["bridge_failures"], now, BRIDGE_WINDOW))
            >= MAX_FAILURES
        ):
            name = "Pairing temporarily locked"
        else:
            name = "Pairing code active"
        return {
            "status": name,
            "code": code if name == "Pairing code active" else None,
            "expires_at": expires if name == "Pairing code active" else None,
        }

    def status(self, *, paired):
        if paired:
            return {"status": "Paired", "code": None, "expires_at": None}
        with _file_lock(self.path):
            state = self._read()
            now = self._now()
            if state is not None:
                self._check_clock(state, now)
                self._expire(state, now)
            return self._status(state, now)

    def generate_new_code(self, *, paired):
        if paired:
            raise OnboardingError("already_paired")
        with _file_lock(self.path):
            now = self._now()
            state = self._read() or _default()
            self._check_clock(state, now)
            self._prune(state, now)
            self._expire(state, now)
            if len(state["bridge_failures"]) >= MAX_FAILURES:
                raise OnboardingError("pairing_temporarily_locked")
            generated = state["last_generated_at"]
            if generated is not None and now - generated < GENERATION_COOLDOWN:
                raise OnboardingError("generation_cooldown")
            if state["transaction"] is not None:
                raise OnboardingError("pairing_in_progress")
            code = self._new_code(state, now)
            self.event("code_replaced")
            return code

    def verify_and_reserve(
        self,
        candidate,
        source,
        target_id,
        request_id,
        *,
        paired,
        bridge_id=None,
        secret=None,
        nonce=None,
        bootstrap_version=None,
    ):
        """Consume a matching OTP and atomically reserve the provisional slot."""
        if paired:
            raise OnboardingError("already_paired")
        try:
            source = str(ipaddress.ip_address(source))
            target_id = str(uuid.UUID(str(target_id)))
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError):
            raise OnboardingError("pairing_request_invalid") from None
        extended = bridge_id is not None or secret is not None or nonce is not None
        if extended:
            try:
                bridge_id = str(uuid.UUID(str(bridge_id)))
            except (TypeError, ValueError, AttributeError):
                raise OnboardingError("pairing_request_invalid") from None
            if (
                not isinstance(secret, bytes)
                or len(secret) != 32
                or not isinstance(nonce, str)
                or len(nonce) != 43
                or bootstrap_version != BOOTSTRAP_VERSION
            ):
                raise OnboardingError("pairing_request_invalid")
        with _file_lock(self.path):
            now = self._now()
            state = self._read()
            if state is None:
                raise OnboardingError("pairing_code_unavailable")
            self._check_clock(state, now)
            self._prune(state, now)
            self._expire(state, now)
            if extended and any(
                item[0] == request_id or item[1] == nonce
                for item in state["bootstrap_replay"]
            ):
                raise OnboardingError("bootstrap_replay_rejected")
            if state["transaction"] is not None:
                raise OnboardingError("pairing_in_progress")
            if state["code"] is None or state["code_expired"]:
                raise OnboardingError("pairing_code_expired")
            if (
                state["failed_attempts"] >= MAX_FAILURES
                or len(state["bridge_failures"]) >= MAX_FAILURES
                or len(state["source_failures"].get(source, [])) >= MAX_FAILURES
            ):
                raise OnboardingError("pairing_temporarily_locked")
            if not isinstance(candidate, str) or not hmac.compare_digest(
                state["code"], candidate
            ):
                state["failed_attempts"] += 1
                state["bridge_failures"].append(now)
                state["source_failures"].setdefault(source, []).append(now)
                if extended:
                    state["bootstrap_replay"].append([request_id, nonce, now])
                self._write(state)
                self.event("incorrect_code")
                if state["failed_attempts"] >= MAX_FAILURES:
                    self.event("code_locked")
                return False
            state.update(
                code=None, code_expired=False, created_at=None, expires_at=None
            )
            transaction = {
                "target_id": target_id,
                "request_id": request_id,
                "expires_at": now + PROVISIONAL_TTL,
            }
            if extended:
                transaction.update(
                    bridge_id=bridge_id,
                    secret=secret.hex(),
                    credential_digest=hashlib.sha256(secret).hexdigest(),
                    bootstrap_version=bootstrap_version,
                    nonce=nonce,
                    replay=[],
                )
                state["bootstrap_replay"].append([request_id, nonce, now])
            state["transaction"] = transaction
            self._write(state)
            self.event("code_consumed")
            return True

    def provisional(self, target_id, bridge_id):
        with _file_lock(self.path):
            state = self._read()
            if state is None:
                raise OnboardingError("provisional_expired")
            now = self._now()
            transaction = state["transaction"]
            if transaction is not None and now >= transaction["expires_at"]:
                state["transaction"] = None
                self._write(state)
                self.event("provisional_expired")
                raise OnboardingError("provisional_expired")
            if (
                transaction is None
                or "secret" not in transaction
                or transaction["target_id"] != str(target_id)
                or transaction["bridge_id"] != str(bridge_id)
            ):
                raise OnboardingError("provisional_expired")
            return dict(transaction)

    def claim_provisional_request(self, target_id, bridge_id, request_id, nonce):
        with _file_lock(self.path):
            state = self._read()
            if state is None:
                raise OnboardingError("provisional_expired")
            now = self._now()
            transaction = state["transaction"]
            if transaction is None or now >= transaction["expires_at"]:
                if transaction is not None:
                    state["transaction"] = None
                    self._write(state)
                    self.event("provisional_expired")
                raise OnboardingError("provisional_expired")
            if (
                "secret" not in transaction
                or transaction["target_id"] != str(target_id)
                or transaction["bridge_id"] != str(bridge_id)
            ):
                raise OnboardingError("provisional_expired")
            replay = transaction["replay"]
            if any(item[0] == str(request_id) or item[1] == nonce for item in replay):
                raise OnboardingError("bootstrap_replay_rejected")
            if len(replay) >= MAX_BOOTSTRAP_REPLAY:
                raise OnboardingError("bootstrap_replay_rejected")
            replay.append([str(request_id), nonce, now])
            self._write(state)
            return dict(transaction)

    def clear_provisional(self, target_id, bridge_id, bootstrap_request_id):
        with _file_lock(self.path):
            state = self._read()
            if state is None:
                return False
            if state["transaction"] is None:
                return state["committed_request_id"] == str(bootstrap_request_id)
            transaction = state["transaction"]
            if (
                transaction.get("target_id") != str(target_id)
                or transaction.get("bridge_id") != str(bridge_id)
                or transaction.get("request_id") != str(bootstrap_request_id)
            ):
                return False
            state["transaction"] = None
            state["committed_request_id"] = str(bootstrap_request_id)
            self._write(state)
            return True


def _window_replay(values, now):
    return [item for item in values if now - BOOTSTRAP_REPLAY_TTL < item[2] <= now]
