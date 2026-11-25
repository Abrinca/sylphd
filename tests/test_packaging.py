"""Packaging: the version pins, the two packages and the pure-Python wrapper.

``sylphd`` and ``sylphd-bin`` are released together: the extras pin the binary wheel to
the exact ``sylphd`` version, ``sylphd-bin`` takes its version from the Rust workspace
(``rust/Cargo.toml``), which a test in ``sylphd-client`` keeps equal to
``sylphd.__version__``. This file checks the Python side of that contract without
building anything.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from sylphd import __version__

ROOT = Path(__file__).resolve().parent.parent


def _toml(*parts: str) -> dict:
    path = ROOT.joinpath(*parts)
    return tomllib.loads(path.read_text())


PYPROJECT = _toml("pyproject.toml")
BIN_PYPROJECT = _toml("rust", "sylphd-client", "pyproject.toml")
CARGO = _toml("rust", "Cargo.toml")


class TestVersions:
    def test_the_binary_wheel_is_pinned_to_this_version(self) -> None:
        extras = PYPROJECT["project"]["optional-dependencies"]
        assert extras["client"] == [f"sylphd-bin=={__version__}"]
        assert f"sylphd-bin=={__version__}" in extras["server"]

    def test_the_rust_workspace_has_the_same_version(self) -> None:
        assert CARGO["workspace"]["package"]["version"] == __version__

    def test_the_installed_binary_package_has_the_same_version(self) -> None:
        version = pytest.importorskip("importlib.metadata").version
        try:
            installed = version("sylphd-bin")
        except ImportError:  # PackageNotFoundError: a bare source checkout
            pytest.skip("sylphd-bin is not installed")
        assert installed == __version__
