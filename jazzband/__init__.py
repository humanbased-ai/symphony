"""Jazzband Python implementation."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

__all__ = ["__version__"]

try:
    __version__ = version("jazzband")
except PackageNotFoundError:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    __version__ = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
