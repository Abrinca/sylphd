"""sylphd: a long-running sylph server and its command line."""

from .clientbin import ClientRunError, run

__version__ = "0.8.0"

__all__ = ["ClientRunError", "__version__", "run"]
