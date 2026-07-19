"""Uro Engine core library. Architecture: docs/01-architecture.md."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uro-core")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0+unknown"
