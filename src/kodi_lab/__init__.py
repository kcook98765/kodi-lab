"""Kodi Lab application package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kodi-lab")
except PackageNotFoundError:
    __version__ = "1.0.0rc1"

__all__ = ["__version__"]
