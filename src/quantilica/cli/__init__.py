"""Unified CLI for Quantilica open data fetchers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quantilica-cli")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
