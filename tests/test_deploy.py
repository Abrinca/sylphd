"""The deploy scripts: rendering of the systemd database arguments.

``setup-server.sh`` turns ``SYLPH_DB`` (one file, or a space-separated list of
``FILE``/``NAME=FILE`` entries) into the ``--db`` arguments the unit expands.
``SYLPHD_DRY_RUN=1`` prints the generated drop-in without touching the host,
so these tests need no root and no systemd (only bash).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parent.parent / "deploy"
SCRIPT = DEPLOY / "setup-server.sh"


def _run(tmp_path: Path, **site: str) -> subprocess.CompletedProcess[str]:
    """Run the dry-run mode with ``site.env`` values, database dir ``tmp_path``."""
    site_env = tmp_path / "site.env"
    site_env.write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in site.items())
    )
    env = {
        **os.environ,
        "SYLPHD_SITE_ENV": str(site_env),
        "SYLPHD_DRY_RUN": "1",
        "SYLPH_DB_DIR": str(tmp_path),
        "DOMAIN": "example.org",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _touch(tmp_path: Path, *names: str) -> None:
    for name in names:
        (tmp_path / name).write_text("")


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def test_single_database_is_unchanged(tmp_path: Path) -> None:
    _touch(tmp_path, "gtdb.syl2db")
    result = _run(tmp_path, SYLPH_DB="gtdb.syl2db")
    assert result.returncode == 0, result.stderr
    assert (
        f'Environment="SYLPH_DB_ARGS=--db {tmp_path}/gtdb.syl2db"' in result.stdout
    )


def test_several_databases_with_aliases(tmp_path: Path) -> None:
    _touch(tmp_path, "gtdb.syl2db", "uhgv.syldb", "fungi.syldb")
    result = _run(
        tmp_path,
        SYLPH_DB=f"gtdb=gtdb.syl2db uhgv=uhgv.syldb {tmp_path}/fungi.syldb",
    )
    # A path is refused; the third entry must be a file name in the db dir.
    assert result.returncode == 1
    assert "not a path" in result.stderr

    result = _run(tmp_path, SYLPH_DB="gtdb=gtdb.syl2db uhgv=uhgv.syldb fungi.syldb")
    assert result.returncode == 0, result.stderr
    assert (
        'Environment="SYLPH_DB_ARGS='
        f"--db gtdb={tmp_path}/gtdb.syl2db "
        f"--db uhgv={tmp_path}/uhgv.syldb "
        f"--db {tmp_path}/fungi.syldb\""
    ) in result.stdout


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    result = _run(tmp_path, SYLPH_DB="gtdb=gtdb.syl2db")
    assert result.returncode == 1
    assert f"SYLPH_DB file not found: {tmp_path}/gtdb.syl2db" in result.stderr
