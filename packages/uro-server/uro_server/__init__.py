"""Uro Engine server: transport, sessions, wiring — no engine logic. Governing doc: 08."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uro-server")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0+unknown"
