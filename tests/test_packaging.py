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
        except Exception:  # not installed (a bare source checkout)
            pytest.skip("sylphd-bin is not installed")
        assert installed == __version__


class TestPackages:
    def test_the_base_install_is_httpx_only(self) -> None:
        assert PYPROJECT["project"]["dependencies"] == ["httpx"]
        # The taxonomy extra needs nothing else (NCBI lookups are HTTPS and SQLite).
        assert PYPROJECT["project"]["optional-dependencies"]["taxonomy"] == []

    def test_the_server_extra_is_the_binary_plus_the_server(self) -> None:
        server = PYPROJECT["project"]["optional-dependencies"]["server"]
        assert server == [
            f"sylphd-bin=={__version__}",
            "dnaio",
            "pysylph",
            "python-multipart",
            "starlette",
            "uvicorn",
        ]

    def test_a_binary_only_wheel(self) -> None:
        """maturin ``bindings = "bin"``: py3-none-<platform>, no extension module."""
        assert BIN_PYPROJECT["project"]["name"] == "sylphd-bin"
        assert BIN_PYPROJECT["tool"]["maturin"]["bindings"] == "bin"
        assert BIN_PYPROJECT["build-system"]["build-backend"] == "maturin"
        # A binary wheel serves every Python 3.
        assert BIN_PYPROJECT["project"]["requires-python"] == ">=3.8"
        # The binary comes from the crate, not from a second Cargo.toml.
        assert "version" not in BIN_PYPROJECT["project"]
        assert BIN_PYPROJECT["project"]["dynamic"] == ["version"]

    def test_the_wrapper_is_a_pure_python_wheel(self) -> None:
        assert PYPROJECT["build-system"]["build-backend"] == "hatchling.build"
        wheel = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]
        assert wheel["packages"] == ["src/sylphd"]
        assert PYPROJECT["project"]["scripts"] == {"sylphd": "sylphd.entry:main"}
