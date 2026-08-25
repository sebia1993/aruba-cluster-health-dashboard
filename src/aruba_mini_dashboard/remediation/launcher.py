"""Backward-compatible entry point without runtime MainWindow monkey patching."""

from __future__ import annotations


def install_main_window_extension() -> None:
    """Retained for API compatibility; composition now happens in main.main."""


def main(argv: list[str] | None = None) -> int:
    from aruba_mini_dashboard.main import main as application_main

    return int(application_main(argv))
