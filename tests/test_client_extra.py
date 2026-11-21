"""The client extra works without the server extra (no starlette, no uvicorn)."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_client_commands_import_without_server_packages() -> None:
    # Run in a fresh interpreter where the server packages cannot be imported,
    # as with `sylphd[client]` alone.
    code = textwrap.dedent(
        """
        import sys

        class Block:
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in {"starlette", "uvicorn", "multipart", "python_multipart"}:
                    raise ImportError(f"blocked {name}", name=name)

        sys.meta_path.insert(0, Block())
        from sylphd.entry import main
        raise SystemExit(main(["info", "--help"]))
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "usage: sylphd info" in result.stdout
