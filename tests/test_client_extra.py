"""The base install and ``sylphd[client]`` work without the server packages."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_entry_point_imports_without_server_or_sketching_packages() -> None:
    # Run in a fresh interpreter where everything an extra installs cannot be imported:
    # the entry point, `--version`, `--help` and the Python-only commands must still work.
    code = textwrap.dedent(
        """
        import sys

        BLOCKED = {"starlette", "uvicorn", "multipart", "python_multipart", "pysylph", "dnaio"}

        class Block:
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ImportError(f"blocked {name}", name=name)

        sys.meta_path.insert(0, Block())
        from sylphd.entry import main
        assert main(["--version"]) == 0
        assert main(["taxmatrix", "--help"]) == 0
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_serve_without_server_packages_names_the_extra() -> None:
    code = textwrap.dedent(
        """
        import sys

        class Block:
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in {"starlette", "uvicorn"}:
                    raise ImportError(f"blocked {name}", name=name)

        sys.meta_path.insert(0, Block())
        from sylphd.entry import main
        raise SystemExit(main(["serve", "--db", "x.syldb"]))
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "pip install 'sylphd[server]'" in result.stderr
