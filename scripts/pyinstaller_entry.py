"""Absolute-import entry point for PyInstaller.

Keeping the executable bootstrap outside the package avoids running package
modules as package-less scripts.  The remediation launcher installs the
independent opt-in UI extension before loading the established application
runtime.
"""

from aruba_mini_dashboard.remediation.launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
