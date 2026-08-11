"""Absolute-import entry point for PyInstaller.

Keeping the executable bootstrap outside the package avoids running
``aruba_mini_dashboard.main`` as a package-less script, which would break its
relative imports in a frozen application.
"""

from aruba_mini_dashboard.main import main


if __name__ == "__main__":
    raise SystemExit(main())
