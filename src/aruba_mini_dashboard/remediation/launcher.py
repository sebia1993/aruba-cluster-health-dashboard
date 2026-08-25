"""Application entry point that installs the optional remediation UI extension.

The original dashboard runtime remains authoritative for health collection.  The
extension is installed before :mod:`aruba_mini_dashboard.main` binds MainWindow,
so packaged and console entry points receive the independent opt-in remediation
panel without changing the established read-only collector classes.
"""

from __future__ import annotations

import logging
from typing import Any


LOGGER = logging.getLogger(__name__)
_PATCH_MARKER = "_automatic_remediation_extension_installed"


def install_main_window_extension() -> None:
    from aruba_mini_dashboard.ui import main_window as module

    if bool(getattr(module, _PATCH_MARKER, False)):
        return
    base_window = module.MainWindow

    class RemediationMainWindow(base_window):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.remediation_controller = None
            try:
                from .controller import RemediationFeatureController

                self.remediation_controller = RemediationFeatureController(self)
            except Exception:
                # Failure of the optional changing-command surface must never
                # prevent the established read-only dashboard from starting.
                LOGGER.exception("Automatic remediation extension failed to initialize")
                self.statusBar().showMessage(
                    "자동 장애조치 기능을 초기화하지 못했습니다. 기존 읽기 전용 점검은 계속 사용할 수 있습니다.",
                    15_000,
                )

    RemediationMainWindow.__name__ = base_window.__name__
    RemediationMainWindow.__qualname__ = base_window.__qualname__
    RemediationMainWindow.__module__ = base_window.__module__
    module.MainWindow = RemediationMainWindow
    setattr(module, _PATCH_MARKER, True)


def main(argv: list[str] | None = None) -> int:
    install_main_window_extension()
    from aruba_mini_dashboard import main as application

    return int(application.main(argv))
