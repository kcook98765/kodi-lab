"""No-follow validation of registered local Kodi add-on source directories."""

from __future__ import annotations

import os
import stat
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kodi_lab.development.confined_fs import (
    open_development_root,
    open_source_component,
)
from kodi_lab.domain import (
    MAX_ADDON_NAME,
    MAX_ADDON_VERSION,
    normalize_development_source_locator,
    normalize_extension_types,
    validate_addon_id,
    validate_observed_metadata,
)

MAX_ADDON_XML_BYTES = 1024 * 1024
MAX_DEPENDENCIES = 256
MAX_DEPENDENCY_VERSION = 128
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW

_PYTHON_EXTENSION_POINTS = frozenset(
    {
        "xbmc.python.library",
        "xbmc.python.lyrics",
        "xbmc.python.module",
        "xbmc.python.pluginsource",
        "xbmc.python.script",
        "xbmc.python.weather",
    }
)
_AUTO_START_EXTENSION_POINTS = frozenset({"xbmc.service"})


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
    dependencies: tuple[DevelopmentSourceDependency, ...]
    extensions: tuple[DevelopmentSourceExtension, ...]

    @property
    def is_python_addon(self) -> bool:
        """Use declared extension semantics, never source filenames."""

        return any(point in _PYTHON_EXTENSION_POINTS for point in self.extension_types)

    @property
    def has_auto_start_behavior(self) -> bool:
        """Identify the first-slice extension points that can run on enable."""

        return any(
            point in _AUTO_START_EXTENSION_POINTS for point in self.extension_types
        )


@dataclass(frozen=True, slots=True)
class DevelopmentSourceDependency:
    """Normalized declaration truth from one trusted source descriptor."""

    dependency_id: str
    version: str | None
    minversion: str | None
    optional: bool


@dataclass(frozen=True, slots=True)
class DevelopmentSourceExtension:
    """One current descriptor extension used only for bounded policy checks."""

    point: str
    library: str | None
    provides: tuple[str, ...]


def copied_video_plugin_library(
    observation: DevelopmentSourceValidationResult,
) -> str | None:
    """Return the sole safe Python video-plugin library, else no authority."""

    executable = [
        extension
        for extension in observation.extensions
        if extension.point != "xbmc.addon.metadata"
    ]
    if (
        len(executable) != 1
        or executable[0].point != "xbmc.python.pluginsource"
        or executable[0].provides != ("video",)
        or executable[0].library is None
    ):
        return None
    library = executable[0].library
    try:
        encoded = library.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    components = library.split("/")
    if (
        not library
        or library.startswith("/")
        or library.endswith("/")
        or "\\" in library
        or len(encoded) > 512
        or len(components) > 32
        or any(
            not component
            or component in {".", ".."}
            or len(component.encode("utf-8")) > 255
            or any(unicodedata.category(character) == "Cc" for character in component)
            for component in components
        )
    ):
        return None
    return library


class DevelopmentSourceValidator:
    """Validate only the source root and its exact root-level addon.xml."""

    def __init__(self, development_root: Path | None) -> None:
        self._development_root = development_root

    def root_available(self) -> bool:
        """Report whether the configured root can be opened without following links."""

        try:
            root_fd = self._open_root()
        except DevelopmentSourceValidationError:
            return False
        os.close(root_fd)
        return True

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
        try:
            return open_development_root(root)
        except ValueError as error:
            raise DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ROOT_INVALID,
                "development root must be a normalized non-root absolute path",
            ) from error
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
                    next_fd = open_source_component(
                        current_fd, component, _DIRECTORY_FLAGS
                    )
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
            addon_fd = open_source_component(source_fd, "addon.xml", _FILE_FLAGS)
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
            extensions = tuple(
                DevelopmentSourceExtension(
                    point=child.attrib.get("point", ""),
                    library=child.attrib.get("library"),
                    provides=tuple(
                        token
                        for grandchild in child
                        if grandchild.tag == "provides"
                        for token in (grandchild.text or "").strip().split()
                    ),
                )
                for child in root
                if child.tag == "extension"
            )
            dependencies = DevelopmentSourceValidator._parse_dependencies(root)
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
            dependencies=dependencies,
            extensions=extensions,
        )

    @staticmethod
    def _parse_dependencies(
        root: ET.Element,
    ) -> tuple[DevelopmentSourceDependency, ...]:
        requires_nodes = [child for child in root if child.tag == "requires"]
        if len(requires_nodes) > 1:
            raise ValueError("addon.xml must contain at most one requires element")
        for child in root.iter("import"):
            if not requires_nodes or child not in list(requires_nodes[0]):
                raise ValueError("dependency imports must be direct requires children")
        if not requires_nodes:
            return ()
        requires = requires_nodes[0]
        dependencies: list[DevelopmentSourceDependency] = []
        seen: set[str] = set()
        for child in requires:
            if child.tag != "import" or list(child) or (child.text or "").strip():
                raise ValueError("requires may contain only empty import elements")
            if not set(child.attrib) <= {"addon", "version", "minversion", "optional"}:
                raise ValueError(
                    "dependency declaration contains unsupported attributes"
                )
            dependency_id = validate_addon_id(child.attrib.get("addon", ""))
            if dependency_id in seen:
                raise ValueError("dependency IDs must be unique")
            seen.add(dependency_id)
            version = DevelopmentSourceValidator._dependency_version(
                child.attrib.get("version"), "version"
            )
            minversion = DevelopmentSourceValidator._dependency_version(
                child.attrib.get("minversion"), "minversion"
            )
            raw_optional = child.attrib.get("optional")
            if raw_optional is None or raw_optional == "false":
                optional = False
            elif raw_optional == "true":
                optional = True
            else:
                raise ValueError("dependency optional must be literal true or false")
            dependencies.append(
                DevelopmentSourceDependency(
                    dependency_id=dependency_id,
                    version=version,
                    minversion=minversion,
                    optional=optional,
                )
            )
            if len(dependencies) > MAX_DEPENDENCIES:
                raise ValueError("addon.xml dependency count exceeds the fixed limit")
        return tuple(sorted(dependencies, key=lambda item: item.dependency_id))

    @staticmethod
    def _dependency_version(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        return validate_observed_metadata(
            value,
            field=f"dependency {field}",
            maximum=MAX_DEPENDENCY_VERSION,
        )


def parse_captured_addon_xml(
    relative_locator: str, document: bytes
) -> DevelopmentSourceValidationResult:
    """Parse broker-captured root addon.xml bytes under the catalog contract."""

    return DevelopmentSourceValidator._parse_addon_xml(relative_locator, document)
