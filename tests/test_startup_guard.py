from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from aruba_mini_dashboard.config import AppPathError, AppPaths


def _main_module():
    return importlib.import_module("aruba_mini_dashboard.main")


def test_app_paths_reports_sanitized_action_when_data_root_is_a_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocked_root = tmp_path / "private-operator-profile" / "blocked-state"
    blocked_root.parent.mkdir(parents=True)
    blocked_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(blocked_root))

    with pytest.raises(AppPathError) as raised:
        AppPaths.from_environment().ensure()

    message = str(raised.value)
    assert "데이터 폴더" in message
    assert "쓰기 권한" in message
    assert str(blocked_root) not in message


def test_startup_path_failure_is_reported_before_sqlite_is_opened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main_module = _main_module()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    blocked_root = tmp_path / "operator-name-must-not-be-shown"
    blocked_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(blocked_root))
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_module.QMessageBox,
        "critical",
        lambda _parent, title, message: notices.append((title, message)),
    )
    monkeypatch.setattr(
        main_module,
        "SQLiteStorage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("filesystem failure must stop before SQLite")
        ),
    )

    assert main_module.main([]) == 2

    assert notices
    assert notices[0][0] == "프로그램 데이터 폴더 확인 필요"
    assert "쓰기 권한" in notices[0][1]
    assert str(blocked_root) not in notices[0][1]


def test_live_instance_guard_is_per_data_root_and_releases_cleanly(tmp_path: Path) -> None:
    main_module = _main_module()
    first_paths = AppPaths.from_environment(tmp_path / "first").ensure()
    second_paths = AppPaths.from_environment(tmp_path / "두 번째 data root").ensure()
    first_lock = main_module._acquire_instance_lock(first_paths)
    second_lock = None
    try:
        assert first_lock.staleLockTime() == 0
        with pytest.raises(main_module.InstanceAlreadyRunningError):
            main_module._acquire_instance_lock(first_paths)
        second_lock = main_module._acquire_instance_lock(second_paths)
    finally:
        if second_lock is not None:
            second_lock.unlock()
        first_lock.unlock()

    reacquired = main_module._acquire_instance_lock(first_paths)
    reacquired.unlock()


def test_instance_guard_recovers_a_lock_left_by_crashed_process(tmp_path: Path) -> None:
    main_module = _main_module()
    paths = AppPaths.from_environment(tmp_path).ensure()
    lock_path = paths.root / main_module._INSTANCE_LOCK_FILENAME
    script = (
        "import os, sys\n"
        "from PySide6.QtCore import QLockFile\n"
        "lock = QLockFile(sys.argv[1])\n"
        "lock.setStaleLockTime(0)\n"
        "assert lock.tryLock(0)\n"
        "os._exit(0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(lock_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert lock_path.exists()

    recovered = main_module._acquire_instance_lock(paths)
    recovered.unlock()


@pytest.mark.parametrize("argv", ([], ["--demo"]))
def test_second_process_exits_before_opening_sqlite_or_polling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
) -> None:
    main_module = _main_module()
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(tmp_path))
    paths = AppPaths.from_environment().ensure()
    first_lock = main_module._acquire_instance_lock(paths)
    notices: list[tuple[str, str]] = []

    monkeypatch.setattr(
        main_module.QMessageBox,
        "information",
        lambda _parent, title, message: notices.append((title, message)),
    )
    monkeypatch.setattr(
        main_module,
        "SQLiteStorage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("second process must not open SQLite")
        ),
    )
    monkeypatch.setattr(
        main_module,
        "RuntimePoller",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("second process must not create a poller")
        ),
    )
    try:
        assert main_module.main(argv) == 3
    finally:
        first_lock.unlock()

    assert notices
    assert notices[0][0] == "대시보드가 이미 실행 중입니다"
    assert "이미 실행 중" in notices[0][1]
    assert not paths.database.exists()


def test_smoke_modes_do_not_create_or_lock_the_runtime_data_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The complete dependency and Qt smoke behavior is covered elsewhere. This
    # focused check keeps their startup-isolation contract tied to the guard.
    main_module = _main_module()
    root = tmp_path / "must-not-exist"
    monkeypatch.setenv("ARUBA_MINI_DASHBOARD_DATA_DIR", str(root))
    monkeypatch.setattr(main_module, "_run_frozen_smoke", lambda _fixtures: "SMOKE_OK\n")

    assert main_module.main(["--smoke"]) == 0
    assert not root.exists()
