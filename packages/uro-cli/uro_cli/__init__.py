"""Uro Engine CLI reference client. Command surface: docs/08."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uro-cli")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0+unknown"
