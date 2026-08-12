"""Application configuration and filesystem locations.

Only non-secret settings are represented here.  User names, passwords and
enable secrets are stored by :mod:`aruba_mini_dashboard.credentials` and this
module persists opaque credential identifiers only.
"""

from __future__ import annotations

import ipaddress
import json
import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


APP_DIRECTORY_NAME = "ArubaMiniDashboard"
SETTINGS_SCHEMA_VERSION = 1
LOW_SPEC_MIN_INTERVAL_SECONDS = 120


class SettingsError(RuntimeError):
    """Base class for actionable settings failures."""


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
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
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
            _reject_secret_fields(value)
            mm = _mapping(value.get("mobility_master", {}), "mobility_master")
            cluster = _mapping(value.get("cluster", {}), "cluster")
            members_raw = cluster.get("members", [])
            if not isinstance(members_raw, list):
                raise TypeError("cluster.members must be a list")
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


class SettingsStore:
    """Atomic JSON store for non-secret application settings."""

    def __init__(self, path_or_paths: str | os.PathLike[str] | AppPaths | None = None):
        if isinstance(path_or_paths, AppPaths):
            self.path = path_or_paths.settings
        elif path_or_paths is None:
            self.path = default_app_paths().settings
        else:
            self.path = Path(path_or_paths)

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings.default()
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            _reject_secret_fields(payload)
            settings = AppSettings.from_dict(payload)
            settings.validate()
            return settings
        except SettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SettingsCorruptError(
                f"설정 파일을 읽을 수 없습니다. 원본을 보존한 채 확인하세요: {self.path}"
            ) from exc
        except Exception as exc:
            # JSON values are untrusted input. Unexpected schema failures must
            # not escape startup as AttributeError/TypeError, and the raw value
            # must never be repeated in the operator-facing error.
            raise SettingsCorruptError(
                f"설정 파일 항목의 형식을 확인할 수 없습니다. 원본은 보존됩니다: {self.path}"
            ) from exc

    def save(self, settings: AppSettings) -> Path:
        settings.validate()
        payload = settings.to_dict()
        _reject_secret_fields(payload)
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            return self.path
        except OSError as exc:
            raise SettingsError(f"설정 파일을 안전하게 저장하지 못했습니다: {self.path}") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return dict(value)


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


_FORBIDDEN_SECRET_KEYS = {"password", "passwd", "secret", "enable_secret", "credential_blob", "token"}


def _reject_secret_fields(value: object, path: str = "settings") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if (
                normalized in _FORBIDDEN_SECRET_KEYS
                or normalized.endswith("_password")
                or normalized.endswith("_token")
            ):
                raise SettingsValidationError([f"비밀 값은 설정 파일에 저장할 수 없습니다: {path}.{key}"])
            _reject_secret_fields(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_secret_fields(nested, f"{path}[{index}]")
