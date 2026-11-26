"""Checking an omnitax file (``serve``/``setup --taxonomy-db``) before the expensive work starts."""

from __future__ import annotations

import os

BUILD = "omnitax build -o FILE ncbi+assemblies+gtdb:232 gtdb:232"


def check_taxonomy_file(path: str | os.PathLike[str]) -> None:
    """Raise a ValueError that says what to do when ``path`` is not an omnitax file this omnitax reads."""
    import omnitax

    try:
        omnitax.taxonomies(path)
    except FileNotFoundError as error:
        raise ValueError(f"taxonomy file {path} not found; build it with: {BUILD}") from error
    except omnitax.LayoutError as error:
        if error.layout is None:
            raise ValueError(f"{path} is not an omnitax file (an old sylphd taxdb file?); build one with: {BUILD}") from error
        raise ValueError(f"{path} was built by another omnitax layout ({error.layout}); rebuild it with this omnitax version: {BUILD}") from error
