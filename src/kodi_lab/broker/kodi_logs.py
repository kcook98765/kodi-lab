"""Fixed-path, bounded Kodi log windows for promotion verification only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import docker  # type: ignore[import-untyped]
from docker.errors import DockerException, NotFound  # type: ignore[import-untyped]
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.provisioning import RUNTIME_REVISION_LABEL_KEY
from kodi_lab.config import Settings
from kodi_lab.domain import (
    AgentWorkspacePromotionOperationState,
    PromotionVerificationState,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    AgentWorkspacePromotionHandoffRepository,
    AgentWorkspacePromotionOperationRepository,
    ManagedInstanceRepository,
    PromotionVerificationOperationRepository,
)

KODI_LOG_DIRECTORY = "/home/kodi/.kodi/temp"
APPROVED_LOG_NAMES = ("kodi.log", "kodi.old.log")
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_NEW_LOG_BYTES = 1024 * 1024
MAX_RETAINED_BYTES = 40 * 1024
MAX_SNIPPETS = 20
MAX_SNIPPET_BYTES = 512
MAX_WEB_SNIPPET_BYTES = 8 * 1024
_CURSOR_KEY = secrets.token_bytes(32)


class KodiLogWindowError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


@dataclass(frozen=True, slots=True)
class KodiLogAuthority:
    verification_id: str
    instance_id: str
    container_id: str
    runtime_revision_id: str
    addon_id: str


@dataclass(frozen=True, slots=True)
class KodiLogWindow:
    complete: bool
    classification: str
    target_error_count: int
    unrelated_warning_count: int
    snippets: tuple[str, ...]
    scanned_bytes: int


class KodiLogAuthorityResolver(Protocol):
    def resolve(
        self, verification_id: str, instance_id: str, container_id: str
    ) -> KodiLogAuthority: ...


class SqliteKodiLogAuthorityResolver:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def production(cls) -> SqliteKodiLogAuthorityResolver:
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve(
        self, verification_id: str, instance_id: str, container_id: str
    ) -> KodiLogAuthority:
        try:
            verification_uuid = UUID(verification_id)
            instance_uuid = UUID(instance_id)
            if (
                str(verification_uuid) != verification_id
                or str(instance_uuid) != instance_id
            ):
                raise ValueError
        except ValueError as error:
            raise KodiLogWindowError(
                "promotion_log_authority_invalid", "Verification identity is invalid."
            ) from error
        with self._session_factory() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                verification_uuid
            )
            instance = ManagedInstanceRepository(session).get(instance_uuid)
            if operation is None or instance is None:
                raise KodiLogWindowError(
                    "promotion_log_authority_invalid",
                    "Verification authority is unavailable.",
                )
            source = AgentWorkspacePromotionOperationRepository(session).get(
                operation.source_promotion_id
            )
            handoff = AgentWorkspacePromotionHandoffRepository(session).get(
                operation.handoff_id
            )
        if (
            source is None
            or source.state is not AgentWorkspacePromotionOperationState.SUCCEEDED
            or handoff is None
            or handoff.verification_operation_id != operation.id
            or handoff.target_instance_id != instance_uuid
            or handoff.trusted_container_id != container_id
            or operation.target_instance_id != instance_uuid
            or operation.runtime_revision_id != instance.runtime_revision_id
            or operation.state
            not in {
                PromotionVerificationState.QUEUED,
                PromotionVerificationState.RUNNING,
            }
        ):
            raise KodiLogWindowError(
                "promotion_log_authority_invalid", "Verification authority drifted."
            )
        return KodiLogAuthority(
            verification_id,
            instance_id,
            container_id,
            str(operation.runtime_revision_id),
            operation.addon_id,
        )


class KodiLogWindowProvider:
    def __init__(self, client_factory: Callable[[], Any] = docker.APIClient) -> None:
        self._client_factory = client_factory

    def _files(self, authority: KodiLogAuthority) -> dict[str, bytes]:
        client = self._client_factory()
        try:
            summaries = client.containers(
                all=True,
                filters={
                    "label": [
                        "io.kodi-lab.managed=true",
                        f"io.kodi-lab.instance-id={authority.instance_id}",
                    ]
                },
            )
            if (
                not isinstance(summaries, list)
                or len(summaries) != 1
                or summaries[0].get("Id") != authority.container_id
            ):
                raise KodiLogWindowError(
                    "promotion_log_container_drift",
                    "Managed container identity changed.",
                )
            inspected = client.inspect_container(authority.container_id)
            labels = (inspected.get("Config") or {}).get("Labels") or {}
            if (
                inspected.get("Id") != authority.container_id
                or labels.get("io.kodi-lab.managed") != "true"
                or labels.get("io.kodi-lab.instance-id") != authority.instance_id
                or labels.get(RUNTIME_REVISION_LABEL_KEY)
                != authority.runtime_revision_id
            ):
                raise KodiLogWindowError(
                    "promotion_log_container_drift",
                    "Managed container identity changed.",
                )
            try:
                stream, _ = client.get_archive(
                    authority.container_id, KODI_LOG_DIRECTORY
                )
            except NotFound:
                return {}
            raw = bytearray()
            for chunk in stream:
                raw.extend(chunk)
                if len(raw) > MAX_ARCHIVE_BYTES:
                    raise KodiLogWindowError(
                        "promotion_log_window_incomplete",
                        "Kodi log archive exceeds the fixed observation bound.",
                    )
            result: dict[str, bytes] = {}
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
                for member in archive.getmembers():
                    name = member.name.rstrip("/").split("/")[-1]
                    if name not in APPROVED_LOG_NAMES:
                        continue
                    if (
                        not member.isfile()
                        or member.size < 0
                        or member.size > MAX_ARCHIVE_BYTES
                    ):
                        raise KodiLogWindowError(
                            "promotion_log_window_incomplete",
                            "Kodi log object is not safely readable.",
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise KodiLogWindowError(
                            "promotion_log_window_incomplete",
                            "Kodi log object is unavailable.",
                        )
                    result[name] = source.read(member.size + 1)
            return result
        except KodiLogWindowError:
            raise
        except (
            DockerException,
            OSError,
            tarfile.TarError,
            ValueError,
            TypeError,
        ) as error:
            raise KodiLogWindowError(
                "promotion_log_window_incomplete",
                "Kodi log observation is unavailable.",
            ) from error
        finally:
            client.close()

    @staticmethod
    def _fingerprint(files: dict[str, bytes]) -> dict[str, dict[str, object]]:
        return {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        }

    def capture(self, authority: KodiLogAuthority) -> str:
        payload = {
            "v": 1,
            "verification": authority.verification_id,
            "instance": authority.instance_id,
            "container": authority.container_id,
            "files": self._fingerprint(self._files(authority)),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signature = hmac.new(_CURSOR_KEY, encoded, hashlib.sha256).hexdigest().encode()
        return (encoded + b"." + signature).decode()

    def read(self, authority: KodiLogAuthority, cursor: str) -> KodiLogWindow:
        try:
            encoded, raw_signature = cursor.encode().split(b".", 1)
            if not hmac.compare_digest(
                hmac.new(_CURSOR_KEY, encoded, hashlib.sha256).hexdigest().encode(),
                raw_signature,
            ):
                raise ValueError
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
            )
            if (
                payload.get("v") != 1
                or payload.get("verification") != authority.verification_id
                or payload.get("instance") != authority.instance_id
                or payload.get("container") != authority.container_id
            ):
                raise ValueError
            prior = payload["files"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise KodiLogWindowError(
                "promotion_log_cursor_invalid", "Kodi log cursor cannot be correlated."
            ) from error
        current = self._files(authority)
        prior_current = prior.get("kodi.log")
        new_data: bytes | None = None
        if prior_current is None:
            new_data = current.get("kodi.log", b"")
        elif "kodi.log" in current and len(current["kodi.log"]) >= int(
            prior_current["size"]
        ):
            size = int(prior_current["size"])
            prefix = current["kodi.log"][:size]
            if hashlib.sha256(prefix).hexdigest() == prior_current["sha256"]:
                new_data = current["kodi.log"][size:]
        if new_data is None and "kodi.old.log" in current and "kodi.log" in current:
            old = current["kodi.old.log"]
            size = int(prior_current["size"])
            if (
                len(old) >= size
                and hashlib.sha256(old[:size]).hexdigest() == (prior_current["sha256"])
            ):
                new_data = old[size:] + current["kodi.log"]
        if new_data is None or len(new_data) > MAX_NEW_LOG_BYTES:
            return KodiLogWindow(
                False,
                "incomplete",
                0,
                0,
                (),
                min(0 if new_data is None else len(new_data), MAX_NEW_LOG_BYTES),
            )
        return classify_kodi_log_window(new_data, authority.addon_id)


def _sanitize(line: str) -> str:
    clean = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in line
    )
    encoded = clean.encode("utf-8", "replace")[:MAX_SNIPPET_BYTES]
    return encoded.decode("utf-8", "ignore")


def classify_kodi_log_window(data: bytes, addon_id: str) -> KodiLogWindow:
    text = data[-MAX_RETAINED_BYTES:].decode("utf-8", "replace")
    target_patterns = (
        "traceback",
        "importerror",
        "modulenotfounderror",
        "python exception",
        "script failed",
        "service failed",
        "dependency",
    )
    unrelated_patterns = ("dbus", "vdpau", "zeroconf")
    targets: list[str] = []
    target_count = warning_count = 0
    correlation_budget = 0
    for line in text.splitlines():
        lowered = line.lower()
        if addon_id.lower() in lowered:
            correlation_budget = 8
        target = correlation_budget > 0 and (
            "error" in lowered or any(pattern in lowered for pattern in target_patterns)
        )
        if target:
            target_count += 1
            snippet = _sanitize(line)
            if (
                len(targets) < MAX_SNIPPETS
                and sum(len(item.encode()) for item in targets) + len(snippet.encode())
                <= MAX_WEB_SNIPPET_BYTES
            ):
                targets.append(snippet)
        elif "error" in lowered and any(
            pattern in lowered for pattern in unrelated_patterns
        ):
            warning_count += 1
        if correlation_budget:
            correlation_budget -= 1
    classification = (
        "target_errors"
        if target_count
        else "unrelated_warnings"
        if warning_count
        else "clean"
    )
    return KodiLogWindow(
        True, classification, target_count, warning_count, tuple(targets), len(data)
    )
