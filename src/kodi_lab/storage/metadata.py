"""SQLAlchemy declarative metadata used by schema migrations."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base for Kodi Lab persistence mappings."""


metadata = Base.metadata
