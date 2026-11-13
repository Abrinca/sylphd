"""Allow ``python -m sylphd``."""

import sys

from .entry import main

if __name__ == "__main__":
    sys.exit(main())
