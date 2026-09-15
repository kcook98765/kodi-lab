"""Narrow repositories for the one administrator and Web sessions."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kodi_lab.auth.models import ADMIN_KEY, WebAdminCredential, WebSession
from kodi_lab.storage.models import WebAdminCredentialRow, WebSessionRow


def _timestamp_to_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class WebAdminCredentialRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> WebAdminCredential | None:
        row = self._session.get(WebAdminCredentialRow, ADMIN_KEY)
        if row is None:
            return None
        return WebAdminCredential(
            admin_key=row.admin_key,
            password_hash=row.password_hash,
            password_updated_at=_timestamp_from_text(row.password_updated_at),
        )

    def set_password_hash(
        self, password_hash: str, *, updated_at: datetime
    ) -> WebAdminCredential:
        row = self._session.get(WebAdminCredentialRow, ADMIN_KEY)
        if row is None:
            row = WebAdminCredentialRow(
                admin_key=ADMIN_KEY,
                password_hash=password_hash,
                password_updated_at=_timestamp_to_text(updated_at),
            )
            self._session.add(row)
        else:
            row.password_hash = password_hash
            row.password_updated_at = _timestamp_to_text(updated_at)
        self._session.flush()
        return WebAdminCredential(ADMIN_KEY, password_hash, updated_at)


class WebSessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, web_session: WebSession) -> None:
        self._session.add(
            WebSessionRow(
                token_digest=web_session.token_digest,
                admin_key=web_session.admin_key,
                csrf_token=web_session.csrf_token,
                created_at=_timestamp_to_text(web_session.created_at),
                last_used_at=_timestamp_to_text(web_session.last_used_at),
                absolute_expires_at=_timestamp_to_text(web_session.absolute_expires_at),
            )
        )
        self._session.flush()

    def get(self, token_digest: str) -> WebSession | None:
        row = self._session.get(WebSessionRow, token_digest)
        return None if row is None else self._from_row(row)

    def touch(self, token_digest: str, *, last_used_at: datetime) -> None:
        row = self._session.get(WebSessionRow, token_digest)
        if row is not None:
            row.last_used_at = _timestamp_to_text(last_used_at)
            self._session.flush()

    def delete(self, token_digest: str) -> bool:
        row = self._session.get(WebSessionRow, token_digest)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def delete_all(self) -> None:
        self._session.execute(delete(WebSessionRow))

    def list_token_digests(self) -> list[str]:
        return list(self._session.scalars(select(WebSessionRow.token_digest)))

    @staticmethod
    def _from_row(row: WebSessionRow) -> WebSession:
        return WebSession(
            token_digest=row.token_digest,
            admin_key=row.admin_key,
            csrf_token=row.csrf_token,
            created_at=_timestamp_from_text(row.created_at),
            last_used_at=_timestamp_from_text(row.last_used_at),
            absolute_expires_at=_timestamp_from_text(row.absolute_expires_at),
        )
