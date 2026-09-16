"""No-follow validation of registered local Kodi add-on source directories."""

from __future__ import annotations

import os
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kodi_lab.domain import (
    MAX_ADDON_NAME,
    MAX_ADDON_VERSION,
    normalize_development_source_locator,
    normalize_extension_types,
    validate_addon_id,
    validate_observed_metadata,
)

MAX_ADDON_XML_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


class DevelopmentSourceValidationCode(StrEnum):
    """Bounded validation failure categories safe for application handling."""

    ROOT_NOT_CONFIGURED = "root_not_configured"
    ROOT_INVALID = "root_invalid"
    ROOT_UNAVAILABLE = "root_unavailable"
    LOCATOR_INVALID = "locator_invalid"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ADDON_XML_MISSING = "addon_xml_missing"
    ADDON_XML_UNSAFE = "addon_xml_unsafe"
    ADDON_XML_TOO_LARGE = "addon_xml_too_large"
    ADDON_XML_MALFORMED = "addon_xml_malformed"
    ADDON_METADATA_INVALID = "addon_metadata_invalid"


class DevelopmentSourceValidationError(ValueError):
    """A local source failed one safe registration-time validation rule."""

    def __init__(self, code: DevelopmentSourceValidationCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DevelopmentSourceValidationResult:
    """Parsed source identity without an effective absolute host path."""

    relative_locator: str
    addon_id: str
    observed_name: str
    observed_version: str
    extension_types: tuple[str, ...]


class DevelopmentSourceValidator:
    """Validate only the source root and its exact root-level addon.xml."""

    def __init__(self, development_root: Path | None) -> None:
        self._development_root = development_root

    def validate(self, relative_locator: str) -> DevelopmentSourceValidationResult:
        """Return a current read-only observation of one confined local source."""

        try:
            locator = normalize_development_source_locator(relative_locator)
        except (TypeError, ValueError) as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.LOCATOR_INVALID,
                "development source locator is not a normalized relative path",
            ) from error

        root_fd = self._open_root()
        try:
            source_fd = self._open_source(root_fd, locator)
            try:
                document = self._read_addon_xml(source_fd)
            finally:
                os.close(source_fd)
        finally:
            os.close(root_fd)
        return self._parse_addon_xml(locator, document)

    def _open_root(self) -> int:
        root = self._development_root
        if root is None:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ROOT_NOT_CONFIGURED,
                "development root is not configured",
            )
        if (
            not root.is_absolute()
            or root == Path("/")
            or root != Path(os.path.normpath(root))
        ):
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ROOT_INVALID,
                "development root must be a normalized non-root absolute path",
            )
        try:
            current_fd = os.open("/", _DIRECTORY_FLAGS)
            try:
                for component in root.parts[1:]:
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    os.close(current_fd)
                    current_fd = next_fd
                return current_fd
            except BaseException:
                os.close(current_fd)
                raise
        except OSError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ROOT_UNAVAILABLE,
                "development root is unavailable, unreadable, or link-unsafe",
            ) from error

    @staticmethod
    def _open_source(root_fd: int, locator: str) -> int:
        try:
            current_fd = os.dup(root_fd)
            try:
                for component in locator.split("/"):
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    os.close(current_fd)
                    current_fd = next_fd
                return current_fd
            except BaseException:
                os.close(current_fd)
                raise
        except OSError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.SOURCE_UNAVAILABLE,
                "development source is unavailable, unreadable, or link-unsafe",
            ) from error

    @staticmethod
    def _read_addon_xml(source_fd: int) -> bytes:
        try:
            addon_fd = os.open("addon.xml", _FILE_FLAGS, dir_fd=source_fd)
        except FileNotFoundError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_XML_MISSING,
                "source must contain a root-level addon.xml regular file",
            ) from error
        except OSError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
                "addon.xml is unreadable or link-unsafe",
            ) from error

        try:
            metadata = os.fstat(addon_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise DevelopmentSourceValidationError(
                    DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
                    "addon.xml must be one non-linked regular file",
                )
            if metadata.st_size > MAX_ADDON_XML_BYTES:
                raise DevelopmentSourceValidationError(
                    DevelopmentSourceValidationCode.ADDON_XML_TOO_LARGE,
                    "addon.xml exceeds the 1 MiB registration limit",
                )
            chunks: list[bytes] = []
            remaining = MAX_ADDON_XML_BYTES + 1
            while remaining:
                chunk = os.read(addon_fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            document = b"".join(chunks)
            if len(document) > MAX_ADDON_XML_BYTES:
                raise DevelopmentSourceValidationError(
                    DevelopmentSourceValidationCode.ADDON_XML_TOO_LARGE,
                    "addon.xml exceeds the 1 MiB registration limit",
                )
            return document
        finally:
            os.close(addon_fd)

    @staticmethod
    def _parse_addon_xml(
        locator: str, document: bytes
    ) -> DevelopmentSourceValidationResult:
        try:
            text = document.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_XML_MALFORMED,
                "addon.xml must be valid UTF-8 XML",
            ) from error
        upper = text.upper()
        if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_XML_UNSAFE,
                "addon.xml must not contain DTD or entity declarations",
            )
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_XML_MALFORMED,
                "addon.xml is malformed",
            ) from error
        if root.tag != "addon":
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
                "addon.xml root element must be addon",
            )

        try:
            addon_id = validate_addon_id(root.attrib.get("id", ""))
            observed_name = validate_observed_metadata(
                root.attrib.get("name", ""), field="name", maximum=MAX_ADDON_NAME
            )
            observed_version = validate_observed_metadata(
                root.attrib.get("version", ""),
                field="version",
                maximum=MAX_ADDON_VERSION,
            )
            extension_types = normalize_extension_types(
                tuple(
                    child.attrib.get("point", "")
                    for child in root
                    if child.tag == "extension"
                )
            )
        except ValueError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_METADATA_INVALID,
                str(error),
            ) from error
        return DevelopmentSourceValidationResult(
            relative_locator=locator,
            addon_id=addon_id,
            observed_name=observed_name,
            observed_version=observed_version,
            extension_types=extension_types,
        )
