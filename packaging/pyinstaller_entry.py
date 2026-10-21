"""Entry point for the standalone (PyInstaller) builds.

``src/main.py`` uses package-relative imports, so freezing it directly as a
top-level script fails at runtime with::

    ImportError: attempted relative import with no known parent package

Importing the package entry point instead keeps the ``src`` package intact
inside the bundle.
"""

from src.main import main
from src.updater import main as updater_main  # keep the updater bundled

__all__ = ["main", "updater_main"]

if __name__ == "__main__":
    main()
