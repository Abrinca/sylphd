"""Checking an omnitax file (``serve``/``setup --taxonomy-db``) before the expensive work starts."""

from __future__ import annotations

import os
import re

BUILD = "omnitax build -o FILE ncbi+assemblies+gtdb:232 gtdb:232"


def check_taxonomy_file(path: str | os.PathLike[str]) -> None:
    """Raise a ValueError that says what to do when ``path`` is not an omnitax file this omnitax reads."""
    import omnitax

    try:
        omnitax.taxonomies(path)
    except FileNotFoundError as error:
        raise ValueError(f"taxonomy file {path} not found; build it with: {BUILD}") from error
    except ValueError as error:
        found = re.search(r"layout (\w+)", str(error))
        if found is None:
            raise
        if found.group(1) == "None":
            raise ValueError(f"{path} is not an omnitax file (an old sylphd taxdb file?); build one with: {BUILD}") from error
        raise ValueError(f"{path} was built by another omnitax layout ({found.group(1)}); rebuild it with this omnitax version: {BUILD}") from error
