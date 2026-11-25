"""Path-component hygiene shared by add-on ZIP readers."""

from collections.abc import Iterable

_VCS_COMPONENTS = frozenset({".git", ".hg", ".svn"})


def has_vcs_admin_component(parts: Iterable[str]) -> bool:
    return any(part.casefold() in _VCS_COMPONENTS for part in parts)
