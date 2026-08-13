"""Application configuration and filesystem locations.

Only non-secret settings are represented here.  User names, passwords and
enable secrets are stored by :mod:`aruba_mini_dashboard.credentials` and this
module persists opaque credential identifiers only.
"""

from __future__ import annotations

import ipaddress
import json
import hashlib
import logging
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


APP_DIRECTORY_NAME = "ArubaMiniDashboard"
SETTINGS_SCHEMA_VERSION = 1
LOW_SPEC_MIN_INTERVAL_SECONDS = 120
MAX_SETTINGS_FILE_BYTES = 256 * 1024
MAX_SETTINGS_JSON_DEPTH = 12
MAX_SETTINGS_OBJECT_MEMBERS = 64
MAX_SETTINGS_ARRAY_ITEMS = 128
MAX_SETTINGS_TOTAL_NODES = 1_024
MAX_SETTINGS_UPDATE_MARKER_BYTES = 1_024
MIN_WINDOW_COORDINATE = -100_000
MAX_WINDOW_COORDINATE = 100_000


LOGGER = logging.getLogger(__name__)


class _BoundedFileError(ValueError):
    pass


class SettingsError(RuntimeError):
    """Base class for actionable settings failures."""


class AppPathError(SettingsError):
    """The per-user application data directories could not be prepared."""


class SettingsCorruptError(SettingsError):
    """The settings file exists but cannot be decoded safely."""


class SettingsValidationError(SettingsError):
    """One or more settings are outside their supported bounds."""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    settings: Path
    logs: Path
    app_log: Path
    ssh_debug_log: Path
    performance_log: Path
    known_hosts: Path

    @classmethod
    def from_environment(cls, local_app_data: str | os.PathLike[str] | None = None) -> "AppPaths":
        override = os.environ.get("ARUBA_MINI_DASHBOARD_DATA_DIR", "").strip()
        if override and local_app_data is None:
            root = Path(override)
        else:
            configured = str(local_app_data or os.environ.get("LOCALAPPDATA", "")).strip()
            base = Path(configured) if configured else Path.home() / "AppData" / "Local"
            root = base / APP_DIRECTORY_NAME
        logs = root / "logs"
        return cls(
            root=root,
            database=root / "app.db",
            settings=root / "config" / "settings.json",
            logs=logs,
            app_log=logs / "app.log",
            ssh_debug_log=logs / "ssh_debug.log",
            performance_log=logs / "performance.log",
            known_hosts=root / "known_hosts",
        )

    def ensure(self) -> "AppPaths":
        try:
            # Keep the order deterministic so a failure can be diagnosed while
            # leaving every directory that was already created usable.
            for directory in dict.fromkeys(
                (self.root, self.settings.parent, self.logs, self.known_hosts.parent)
            ):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Do not echo the operator's profile or custom data-root path into
            # an early-startup dialog. The original exception remains chained
            # for a local debugger while the UI receives an actionable message.
            raise AppPathError(
                "프로그램 데이터 폴더를 준비하지 못했습니다. "
                "폴더 쓰기 권한과 디스크 상태를 확인한 뒤 다시 실행하세요."
            ) from exc
        return self


def default_app_paths() -> AppPaths:
    return AppPaths.from_environment()


@dataclass(slots=True)
class ClusterMemberSettings:
    ip: str = ""
    alias: str = ""


@dataclass(slots=True)
class MobilityMasterSettings:
    management_ip: str = ""
    display_name: str = "Aruba Mobility Master"
    ssh_port: int = 22
    credential_id: str = ""
    connect_timeout_seconds: int = 10
    command_timeout_seconds: int = 20
    retries: int = 2
    enable_required: bool = False


@dataclass(slots=True)
class ClusterSettings:
    name: str = "Aruba 7240XM Cluster"
    members: list[ClusterMemberSettings] = field(
        default_factory=lambda: [ClusterMemberSettings() for _ in range(4)]
    )
    primary_controller_ip: str = ""
    fallback_controller_ips: list[str] = field(default_factory=list)
    ssh_port: int = 22
    credential_id: str = ""
    connect_timeout_seconds: int = 10
    command_timeout_seconds: int = 20
    retries: int = 2
    enable_required: bool = False


@dataclass(slots=True)
class CredentialAssignments:
    use_shared_credentials: bool = True
    shared_credential_id: str = ""
    session_only: bool = False

    def effective_id(self, role: str, settings: "AppSettings") -> str:
        if self.use_shared_credentials:
            return self.shared_credential_id.strip()
        if role == "mm":
            return settings.mobility_master.credential_id.strip()
        if role == "cluster":
            return settings.cluster.credential_id.strip()
        raise ValueError(f"Unknown credential role: {role!r}")


@dataclass(slots=True)
class PollingSettings:
    interval_seconds: int = 60
    automatic_enabled: bool = False
    busy_policy: str = "skip"  # Scheduled cycles never overlap.


@dataclass(slots=True)
class DetectionSettings:
    low_client_threshold: int = 10
    anomaly_cycles: int = 3
    recovery_cycles: int = 2
    comparison_mode: str = "absolute_and_relative"
    relative_ratio_percent: int = 25
    minimum_cluster_active_clients: int = 50
    minimum_peer_median: int = 30
    missing_cycles: int = 3


@dataclass(slots=True)
class NotificationSettings:
    notify_new_incidents: bool = True
    repeat_unacknowledged: bool = False
    repeat_interval_minutes: int = 10
    sound_enabled: bool = False
    recovery_notifications: bool = True


@dataclass(slots=True)
class PerformanceSettings:
    """Optional resource-saving behavior; detection rules stay unchanged."""

    low_spec_mode: bool = False
    performance_logging: bool = False


@dataclass(slots=True)
class UiSettings:
    always_on_top: bool = False
    opacity_percent: int = 100
    window_maximized: bool = False
    window_x: int | None = None
    window_y: int | None = None
    window_width: int = 420
    window_height: int = 320


@dataclass(slots=True)
class AppSettings:
    schema_version: int = SETTINGS_SCHEMA_VERSION
    mobility_master: MobilityMasterSettings = field(default_factory=MobilityMasterSettings)
    cluster: ClusterSettings = field(default_factory=ClusterSettings)
    credentials: CredentialAssignments = field(default_factory=CredentialAssignments)
    polling: PollingSettings = field(default_factory=PollingSettings)
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    performance: PerformanceSettings = field(default_factory=PerformanceSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    ssh_debug_logging: bool = False

    @classmethod
    def default(cls) -> "AppSettings":
        return cls()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppSettings":
        if not isinstance(value, Mapping):
            raise SettingsCorruptError("설정 루트는 JSON 객체여야 합니다.")
        try:
            _validate_settings_payload_shape(value)
            _reject_secret_fields(value)
            mm = _mapping(value.get("mobility_master", {}), "mobility_master")
            cluster = _mapping(value.get("cluster", {}), "cluster")
            members_raw = cluster.get("members", [])
            if not isinstance(members_raw, list):
                raise TypeError("cluster.members must be a list")
            if len(members_raw) > 4:
                # Four members is the only schema-valid shape. Reject before
                # constructing arbitrary numbers of dataclass instances.
                raise SettingsCorruptError(
                    "클러스터 구성원 설정은 최대 4개까지만 허용됩니다."
                )
            members = [ClusterMemberSettings(**_mapping(row, "cluster.members[]")) for row in members_raw]
            defaults = cls.default()
            settings = cls(
                schema_version=value.get("schema_version", SETTINGS_SCHEMA_VERSION),
                mobility_master=MobilityMasterSettings(**mm),
                cluster=ClusterSettings(**{**cluster, "members": members or defaults.cluster.members}),
                credentials=CredentialAssignments(**_mapping(value.get("credentials", {}), "credentials")),
                polling=PollingSettings(**_mapping(value.get("polling", {}), "polling")),
                detection=DetectionSettings(**_mapping(value.get("detection", {}), "detection")),
                notifications=NotificationSettings(**_mapping(value.get("notifications", {}), "notifications")),
                performance=PerformanceSettings(**_mapping(value.get("performance", {}), "performance")),
                ui=UiSettings(**_mapping(value.get("ui", {}), "ui")),
                ssh_debug_logging=value.get("ssh_debug_logging", False),
            )
            schema_errors = _settings_schema_errors(settings)
            if schema_errors:
                raise SettingsCorruptError("; ".join(schema_errors))
            return settings
        except SettingsError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            # Do not include the original value in this user-facing message.
            # A malformed setting can contain operational or credential data.
            raise SettingsCorruptError("설정 항목 형식이 올바르지 않습니다.") from exc

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        _reject_secret_fields(result)
        return result

    @property
    def effective_poll_interval_seconds(self) -> int:
        """Interval used by automatic scheduling.

        Manual checks bypass the scheduler, so enabling low-spec mode never
        delays an explicit operator request.
        """

        configured = self.polling.interval_seconds
        if self.performance.low_spec_mode:
            return max(configured, LOW_SPEC_MIN_INTERVAL_SECONDS)
        return configured

    def validate(self) -> None:
        """Validate safe ranges while allowing an unconfigured first run."""

        schema_errors = _settings_schema_errors(self)
        if schema_errors:
            raise SettingsValidationError(schema_errors)
        errors: list[str] = []
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            errors.append(f"지원하지 않는 설정 버전입니다: {self.schema_version}")
        _bounded(errors, "점검 주기", self.polling.interval_seconds, 10, 3600)
        _bounded(errors, "투명도", self.ui.opacity_percent, 40, 100)
        _bounded(errors, "창 너비", self.ui.window_width, 320, 10000)
        _bounded(errors, "창 높이", self.ui.window_height, 240, 10000)
        for label, coordinate in (
            ("창 X 좌표", self.ui.window_x),
            ("창 Y 좌표", self.ui.window_y),
        ):
            if coordinate is not None:
                _bounded(
                    errors,
                    label,
                    coordinate,
                    MIN_WINDOW_COORDINATE,
                    MAX_WINDOW_COORDINATE,
                )
        if self.polling.busy_policy not in {"skip", "queue_one"}:
            errors.append("중복 점검 정책은 skip 또는 queue_one이어야 합니다.")
        if self.detection.comparison_mode not in {"absolute_only", "absolute_and_relative"}:
            errors.append("Client 감지 모드가 올바르지 않습니다.")
        for label, endpoint in (("MM", self.mobility_master), ("클러스터", self.cluster)):
            _bounded(errors, f"{label} SSH 포트", endpoint.ssh_port, 1, 65535)
            _bounded(errors, f"{label} 연결 제한시간", endpoint.connect_timeout_seconds, 1, 600)
            _bounded(errors, f"{label} 명령 제한시간", endpoint.command_timeout_seconds, 1, 600)
            _bounded(errors, f"{label} 재시도 횟수", endpoint.retries, 0, 10)
        for label, number, low, high in (
            ("Low Client Threshold", self.detection.low_client_threshold, 0, 1_000_000_000),
            ("연속 이상 감지 횟수", self.detection.anomaly_cycles, 1, 100),
            ("복구 확인 횟수", self.detection.recovery_cycles, 1, 100),
            ("상대 비교 비율", self.detection.relative_ratio_percent, 1, 100),
            ("클러스터 최소 전체 Client", self.detection.minimum_cluster_active_clients, 0, 1_000_000_000),
            ("Peer 최소 기준값", self.detection.minimum_peer_median, 0, 1_000_000_000),
            ("누락 연속 횟수", self.detection.missing_cycles, 1, 100),
            ("반복 알림 간격", self.notifications.repeat_interval_minutes, 1, 1440),
        ):
            _bounded(errors, label, number, low, high)
        if len(self.cluster.members) != 4:
            errors.append("클러스터 구성원은 정확히 4개여야 합니다.")
        _validate_optional_ips(errors, "MM 관리 IP", [self.mobility_master.management_ip])
        _validate_optional_ips(errors, "클러스터 구성원 IP", [member.ip for member in self.cluster.members])
        _validate_optional_ips(errors, "Primary Controller IP", [self.cluster.primary_controller_ip])
        _validate_optional_ips(errors, "대체 Controller IP", self.cluster.fallback_controller_ips)
        configured_members = [member.ip.strip() for member in self.cluster.members if member.ip.strip()]
        if len(configured_members) != len(set(configured_members)):
            errors.append("클러스터 구성원 IP는 중복될 수 없습니다.")
        fallbacks = [ip.strip() for ip in self.cluster.fallback_controller_ips if ip.strip()]
        if len(fallbacks) != len(set(fallbacks)):
            errors.append("대체 Controller IP는 중복될 수 없습니다.")
        if self.cluster.primary_controller_ip.strip() in fallbacks:
            errors.append("Primary Controller는 대체 Controller 목록에 중복될 수 없습니다.")
        from .credentials import validate_credential_id

        for label, credential_id in (
            ("공통 자격 증명", self.credentials.shared_credential_id),
            ("MM 자격 증명", self.mobility_master.credential_id),
            ("Cluster 자격 증명", self.cluster.credential_id),
        ):
            if not credential_id.strip():
                continue
            try:
                validate_credential_id(credential_id)
            except ValueError:
                errors.append(f"{label} 식별자 형식이 올바르지 않습니다.")
        if errors:
            raise SettingsValidationError(errors)
    def validate_for_monitoring(self) -> None:
        self.validate()
        errors: list[str] = []
        if not self.mobility_master.management_ip.strip():
            errors.append("MM 관리 IP가 필요합니다.")
        member_ips = [member.ip.strip() for member in self.cluster.members]
        if len(member_ips) != 4 or any(not ip for ip in member_ips):
            errors.append("4개의 클러스터 구성원 IP가 모두 필요합니다.")
        primary = self.cluster.primary_controller_ip.strip()
        if not primary:
            errors.append("Primary Controller IP가 필요합니다.")
        elif primary not in member_ips:
            errors.append("Primary Controller IP는 클러스터 구성원 중 하나여야 합니다.")
        allowed = set(member_ips)
        for fallback in self.cluster.fallback_controller_ips:
            if fallback.strip() and fallback.strip() not in allowed:
                errors.append("대체 Controller IP는 클러스터 구성원 중 하나여야 합니다.")
        if not self.credentials.effective_id("mm", self):
            errors.append("MM 자격 증명이 필요합니다.")
        if not self.credentials.effective_id("cluster", self):
            errors.append("클러스터 자격 증명이 필요합니다.")
        if errors:
            raise SettingsValidationError(errors)


def settings_fingerprint(settings: AppSettings) -> str:
    """Stable hash identifying the authoritative, non-secret JSON settings."""

    payload = json.dumps(
        settings.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_bounded_bytes(path: Path, maximum_bytes: int) -> bytes:
    """Read an untrusted local state file without an unbounded allocation."""

    with path.open("rb") as handle:
        encoded = handle.read(maximum_bytes + 1)
    if len(encoded) > maximum_bytes:
        raise _BoundedFileError("file exceeds the allowed size")
    return encoded


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_bounded_json(
    path: Path,
    maximum_bytes: int,
    *,
    encoding: str,
) -> object:
    """Decode bounded local JSON with one strict parser contract."""

    encoded = _read_bounded_bytes(path, maximum_bytes)
    payload = json.loads(
        encoded.decode(encoding),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    _validate_settings_payload_shape(payload)
    return payload


class SettingsStore:
    """Atomic JSON store for non-secret application settings."""

    def __init__(self, path_or_paths: str | os.PathLike[str] | AppPaths | None = None):
        if isinstance(path_or_paths, AppPaths):
            self.path = path_or_paths.settings
        elif path_or_paths is None:
            self.path = default_app_paths().settings
        else:
            self.path = Path(path_or_paths)
        self._rollback_path = self.path.with_name(f".{self.path.name}.rollback")
        self._update_marker_path = self.path.with_name(f".{self.path.name}.update-pending")

    def load(self) -> AppSettings:
        self._recover_interrupted_update()
        if not self.path.exists():
            return AppSettings.default()
        try:
            # Read at most one byte beyond the limit. This remains bounded even
            # if the file changes between an earlier stat and the actual read.
            try:
                payload = _decode_bounded_json(
                    self.path,
                    MAX_SETTINGS_FILE_BYTES,
                    encoding="utf-8",
                )
            except _BoundedFileError:
                raise SettingsCorruptError(
                    "설정 파일이 허용 크기를 초과했습니다. 원본을 보존한 채 설정을 다시 확인하세요."
                )
            settings = AppSettings.from_dict(payload)
            settings.validate()
            return settings
        except SettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SettingsCorruptError(
                "설정 파일을 읽을 수 없습니다. 원본을 보존한 채 확인하세요."
            ) from exc
        except Exception as exc:
            # JSON values are untrusted input. Unexpected schema failures must
            # not escape startup as AttributeError/TypeError, and the raw value
            # must never be repeated in the operator-facing error.
            raise SettingsCorruptError(
                "설정 파일 항목의 형식을 확인할 수 없습니다. 원본은 보존됩니다."
            ) from exc

    def save(self, settings: AppSettings) -> Path:
        self._recover_interrupted_update()
        return self._write_settings(settings)

    def begin_update(self, settings: AppSettings) -> "SettingsUpdate":
        """Stage a settings file update that is committed after runtime apply.

        The rollback copy contains only the already non-secret JSON settings.
        A crash or a second persistence failure leaves the marker in place, so
        the next :meth:`load` restores the exact previous file before parsing.
        """

        self._recover_interrupted_update()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        original_exists = self.path.exists()
        if original_exists:
            try:
                original = _read_bounded_bytes(self.path, MAX_SETTINGS_FILE_BYTES)
            except ValueError as exc:
                raise SettingsCorruptError(
                    "기존 설정 파일이 허용 크기를 초과해 안전하게 갱신할 수 없습니다."
                ) from exc
            except OSError as exc:
                raise SettingsError("기존 설정을 복구용으로 보존하지 못했습니다.") from exc
            self._atomic_write_bytes(self._rollback_path, original)
        marker = json.dumps(
            {"version": 1, "original_exists": original_exists},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        self._atomic_write_bytes(self._update_marker_path, marker)
        try:
            self._write_settings(settings)
        except Exception:
            # Leave the marker/rollback pair intact. The next load, or an
            # explicit rollback below, restores the authoritative old file.
            raise
        return SettingsUpdate(self, original_exists=original_exists)

    def _write_settings(self, settings: AppSettings) -> Path:
        settings.validate()
        payload = settings.to_dict()
        _reject_secret_fields(payload)
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_SETTINGS_FILE_BYTES:
            raise SettingsValidationError(
                ["설정 내용이 저장 가능한 최대 크기를 초과했습니다."]
            )
        self._atomic_write_bytes(self.path, encoded)
        return self.path

    def _recover_interrupted_update(self) -> None:
        if not self._update_marker_path.exists():
            self._remove_orphan_rollback()
            return
        try:
            marker = _decode_bounded_json(
                self._update_marker_path,
                MAX_SETTINGS_UPDATE_MARKER_BYTES,
                encoding="ascii",
            )
            if (
                type(marker) is not dict
                or type(marker.get("version")) is not int
                or marker.get("version") != 1
                or type(marker.get("original_exists")) is not bool
            ):
                raise ValueError("invalid marker")
            if marker["original_exists"]:
                original = _read_bounded_bytes(
                    self._rollback_path,
                    MAX_SETTINGS_FILE_BYTES,
                )
                self._atomic_write_bytes(self.path, original)
            else:
                self.path.unlink(missing_ok=True)
            self._update_marker_path.unlink()
            self._remove_orphan_rollback()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            SettingsCorruptError,
        ) as exc:
            raise SettingsCorruptError(
                "중단된 설정 변경을 복구하지 못했습니다. 원본과 복구 파일을 보존합니다."
            ) from exc

    def _remove_orphan_rollback(self) -> None:
        """Remove a rollback copy after the durable commit marker is gone."""

        try:
            self._rollback_path.unlink(missing_ok=True)
        except OSError:
            # It no longer has recovery authority. A later load retries the
            # cleanup without making otherwise valid settings unavailable.
            LOGGER.warning("Committed settings rollback copy cleanup deferred")

    @staticmethod
    def _atomic_write_bytes(destination: Path, encoded: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            raise SettingsError("설정 파일을 안전하게 저장하지 못했습니다.") from exc
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # A cleanup failure must never replace the actionable write
                    # failure above. The uniquely named file has no recovery
                    # authority and a later save can proceed independently.
                    LOGGER.warning("Temporary settings file cleanup deferred")


class SettingsUpdate:
    """Durable settings update awaiting cross-layer commit or rollback."""

    def __init__(self, store: SettingsStore, *, original_exists: bool) -> None:
        self._store = store
        self._original_exists = bool(original_exists)
        self._finished = False

    def commit(self) -> None:
        if self._finished:
            return
        try:
            self._store._update_marker_path.unlink()
        except OSError as exc:
            raise SettingsError("설정 변경 확정을 기록하지 못했습니다.") from exc
        self._store._remove_orphan_rollback()
        self._finished = True

    def rollback(self) -> None:
        if self._finished:
            return
        self._store._recover_interrupted_update()
        self._finished = True


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return dict(value)


def _validate_settings_payload_shape(value: object) -> None:
    """Bound traversal of untrusted JSON before recursive/schema processing."""

    stack: list[tuple[object, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    total_nodes = 0
    while stack:
        candidate, depth = stack.pop()
        total_nodes += 1
        if total_nodes > MAX_SETTINGS_TOTAL_NODES or depth > MAX_SETTINGS_JSON_DEPTH:
            raise SettingsCorruptError("설정 JSON 구조가 허용 범위를 초과했습니다.")
        if isinstance(candidate, Mapping):
            identity = id(candidate)
            if identity in seen_containers or len(candidate) > MAX_SETTINGS_OBJECT_MEMBERS:
                raise SettingsCorruptError("설정 JSON 객체 구조가 허용 범위를 초과했습니다.")
            seen_containers.add(identity)
            for key, nested in candidate.items():
                if type(key) is not str:
                    raise SettingsCorruptError("설정 JSON 객체 키 형식이 올바르지 않습니다.")
                stack.append((nested, depth + 1))
        elif isinstance(candidate, list):
            identity = id(candidate)
            if identity in seen_containers or len(candidate) > MAX_SETTINGS_ARRAY_ITEMS:
                raise SettingsCorruptError("설정 JSON 배열 구조가 허용 범위를 초과했습니다.")
            seen_containers.add(identity)
            stack.extend((nested, depth + 1) for nested in candidate)
        elif type(candidate) is float:
            if not math.isfinite(candidate):
                raise SettingsCorruptError("설정 JSON 숫자 형식이 올바르지 않습니다.")
        elif candidate is not None and type(candidate) not in {str, int, bool}:
            raise SettingsCorruptError("설정 JSON 값 형식이 올바르지 않습니다.")


def _bounded(errors: list[str], label: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        errors.append(f"{label}은(는) {minimum}~{maximum} 범위의 정수여야 합니다.")


def _settings_schema_errors(settings: AppSettings) -> list[str]:
    """Return strict JSON-schema type errors without coercing any value.

    ``bool`` is a subclass of ``int`` in Python, so every integer and boolean
    check intentionally uses exact types. This keeps strings such as
    ``"false"`` and numbers such as ``0`` from enabling security-sensitive
    behavior through truthiness.
    """

    errors: list[str] = []

    def exact(path: str, value: object, expected: type, description: str) -> bool:
        if type(value) is expected:
            return True
        errors.append(f"{path}은(는) {description} 형식이어야 합니다.")
        return False

    def string(path: str, value: object) -> None:
        exact(path, value, str, "문자열")

    def integer(path: str, value: object) -> None:
        exact(path, value, int, "정수")

    def boolean(path: str, value: object) -> None:
        exact(path, value, bool, "JSON 불리언(true/false)")

    integer("schema_version", settings.schema_version)
    boolean("ssh_debug_logging", settings.ssh_debug_logging)

    mm = settings.mobility_master
    if exact("mobility_master", mm, MobilityMasterSettings, "객체"):
        for field_name in ("management_ip", "display_name", "credential_id"):
            string(f"mobility_master.{field_name}", getattr(mm, field_name))
        for field_name in (
            "ssh_port",
            "connect_timeout_seconds",
            "command_timeout_seconds",
            "retries",
        ):
            integer(f"mobility_master.{field_name}", getattr(mm, field_name))
        boolean("mobility_master.enable_required", mm.enable_required)

    cluster = settings.cluster
    if exact("cluster", cluster, ClusterSettings, "객체"):
        for field_name in ("name", "primary_controller_ip", "credential_id"):
            string(f"cluster.{field_name}", getattr(cluster, field_name))
        for field_name in (
            "ssh_port",
            "connect_timeout_seconds",
            "command_timeout_seconds",
            "retries",
        ):
            integer(f"cluster.{field_name}", getattr(cluster, field_name))
        boolean("cluster.enable_required", cluster.enable_required)
        if exact("cluster.members", cluster.members, list, "배열"):
            for index, member in enumerate(cluster.members):
                if not exact(
                    f"cluster.members[{index}]",
                    member,
                    ClusterMemberSettings,
                    "객체",
                ):
                    continue
                string(f"cluster.members[{index}].ip", member.ip)
                string(f"cluster.members[{index}].alias", member.alias)
        if exact(
            "cluster.fallback_controller_ips",
            cluster.fallback_controller_ips,
            list,
            "배열",
        ):
            for index, fallback in enumerate(cluster.fallback_controller_ips):
                string(f"cluster.fallback_controller_ips[{index}]", fallback)

    credentials = settings.credentials
    if exact("credentials", credentials, CredentialAssignments, "객체"):
        boolean("credentials.use_shared_credentials", credentials.use_shared_credentials)
        boolean("credentials.session_only", credentials.session_only)
        string("credentials.shared_credential_id", credentials.shared_credential_id)

    polling = settings.polling
    if exact("polling", polling, PollingSettings, "객체"):
        integer("polling.interval_seconds", polling.interval_seconds)
        boolean("polling.automatic_enabled", polling.automatic_enabled)
        string("polling.busy_policy", polling.busy_policy)

    detection = settings.detection
    if exact("detection", detection, DetectionSettings, "객체"):
        for field_name in (
            "low_client_threshold",
            "anomaly_cycles",
            "recovery_cycles",
            "relative_ratio_percent",
            "minimum_cluster_active_clients",
            "minimum_peer_median",
            "missing_cycles",
        ):
            integer(f"detection.{field_name}", getattr(detection, field_name))
        string("detection.comparison_mode", detection.comparison_mode)

    notifications = settings.notifications
    if exact("notifications", notifications, NotificationSettings, "객체"):
        for field_name in (
            "notify_new_incidents",
            "repeat_unacknowledged",
            "sound_enabled",
            "recovery_notifications",
        ):
            boolean(f"notifications.{field_name}", getattr(notifications, field_name))
        integer("notifications.repeat_interval_minutes", notifications.repeat_interval_minutes)

    performance = settings.performance
    if exact("performance", performance, PerformanceSettings, "객체"):
        boolean("performance.low_spec_mode", performance.low_spec_mode)
        boolean("performance.performance_logging", performance.performance_logging)

    ui = settings.ui
    if exact("ui", ui, UiSettings, "객체"):
        boolean("ui.always_on_top", ui.always_on_top)
        boolean("ui.window_maximized", ui.window_maximized)
        for field_name in ("opacity_percent", "window_width", "window_height"):
            integer(f"ui.{field_name}", getattr(ui, field_name))
        for field_name in ("window_x", "window_y"):
            candidate = getattr(ui, field_name)
            if candidate is not None:
                integer(f"ui.{field_name}", candidate)

    return errors


def _validate_optional_ips(errors: list[str], label: str, values: list[str]) -> None:
    for value in values:
        candidate = str(value).strip()
        if not candidate:
            continue
        try:
            parsed = ipaddress.ip_address(candidate)
            if parsed.version != 4:
                raise ValueError
        except ValueError:
            errors.append(f"{label} 형식이 올바르지 않습니다: {candidate}")


_FORBIDDEN_SECRET_TOKENS = {
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
}
_FORBIDDEN_SECRET_COMPACT_TOKENS = {
    "accesstoken",
    "apikey",
    "apitoken",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "credentialblob",
    "databasepassword",
    "dbpassword",
    "privatekey",
    "refreshtoken",
    "sessiontoken",
    "userpassword",
}
_FORBIDDEN_SECRET_TOKEN_PAIRS = {
    ("api", "key"),
    ("credential", "blob"),
    ("private", "key"),
}


def _field_name_tokens(key: object) -> tuple[str, ...]:
    """Split separators and camel-case without treating substrings as secrets."""

    value = str(key)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return tuple(
        token.casefold()
        for token in re.split(r"[^A-Za-z0-9]+", value)
        if token
    )


def _is_secret_field_name(key: object) -> bool:
    """Recognize exact secret segments independent of separators or casing."""

    tokens = _field_name_tokens(key)
    if any(token in _FORBIDDEN_SECRET_TOKENS for token in tokens):
        return True
    if any(token in _FORBIDDEN_SECRET_COMPACT_TOKENS for token in tokens):
        return True
    return any(pair in _FORBIDDEN_SECRET_TOKEN_PAIRS for pair in zip(tokens, tokens[1:]))


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_secret_field_name(key):
                raise SettingsValidationError(
                    ["비밀 값은 설정 파일에 저장할 수 없습니다."]
                )
            _reject_secret_fields(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_fields(nested)
