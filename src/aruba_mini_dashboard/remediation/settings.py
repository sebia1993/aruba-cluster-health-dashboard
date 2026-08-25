from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


SETTINGS_SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 32 * 1024


@dataclass(slots=True)
class RemediationSettings:
    schema_version: int = SETTINGS_SCHEMA_VERSION
    enabled: bool = False
    ssh_max_attempts: int = 3
    ssh_retry_interval_seconds: int = 5
    mm_poll_interval_seconds: int = 30
    mm_up_timeout_seconds: int = 20 * 60
    membership_poll_interval_seconds: int = 30
    membership_timeout_seconds: int = 10 * 60
    membership_confirmations: int = 2
    post_poll_interval_seconds: int = 30
    post_timeout_seconds: int = 10 * 60
    post_confirmations: int = 3
    report_timezone: str = "Asia/Seoul"

    def validate(self) -> None:
        errors: list[str] = []
        if type(self.enabled) is not bool:
            errors.append("enabled must be a boolean")
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            errors.append("unsupported remediation settings schema")
        if type(self.ssh_max_attempts) is not int or not 1 <= self.ssh_max_attempts <= 3:
            errors.append("ssh_max_attempts must be between 1 and 3")
        bounds = {
            "ssh_retry_interval_seconds": (1, 60),
            "mm_poll_interval_seconds": (5, 300),
            "mm_up_timeout_seconds": (60, 3600),
            "membership_poll_interval_seconds": (5, 300),
            "membership_timeout_seconds": (60, 1800),
            "membership_confirmations": (1, 10),
            "post_poll_interval_seconds": (5, 300),
            "post_timeout_seconds": (60, 1800),
            "post_confirmations": (1, 10),
        }
        for name, (minimum, maximum) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                errors.append(f"{name} must be between {minimum} and {maximum}")
        if type(self.report_timezone) is not str or not self.report_timezone.strip():
            errors.append("report_timezone must be non-empty text")
        if errors:
            raise ValueError("; ".join(errors))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RemediationSettings":
        if not isinstance(payload, Mapping):
            raise ValueError("remediation settings root must be an object")
        allowed = set(cls.__dataclass_fields__)
        if set(payload) - allowed:
            raise ValueError("unsupported remediation settings field")
        settings = cls(**dict(payload))
        settings.validate()
        return settings

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


class RemediationSettingsStore:
    def __init__(self, app_data_root: str | os.PathLike[str]) -> None:
        self.root = Path(app_data_root) / "remediation"
        self.path = self.root / "settings.json"

    def load(self) -> RemediationSettings:
        if not self.path.exists():
            return RemediationSettings()
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise RuntimeError("자동 장애조치 설정을 읽지 못했습니다.") from exc
        if len(raw) > MAX_SETTINGS_BYTES:
            raise RuntimeError("자동 장애조치 설정 파일이 안전 한도를 초과했습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            return RemediationSettings.from_dict(payload)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("자동 장애조치 설정 형식이 올바르지 않습니다.") from exc

    def save(self, settings: RemediationSettings) -> None:
        encoded = (
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_SETTINGS_BYTES:
            raise RuntimeError("자동 장애조치 설정이 안전 한도를 초과했습니다.")
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=self.root)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise RuntimeError("자동 장애조치 설정을 저장하지 못했습니다.") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
