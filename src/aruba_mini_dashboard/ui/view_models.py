from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping


def value(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def display(value_: Any, default: str = "-") -> str:
    if value_ is None or value_ == "":
        return default
    if isinstance(value_, Enum):
        return str(value_.value)
    if isinstance(value_, datetime):
        if value_.tzinfo is not None and value_.utcoffset() is not None:
            value_ = value_.astimezone()
        return value_.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value_, str) and "T" in value_ and (
        value_.endswith("Z") or "+" in value_[10:] or "-" in value_[10:]
    ):
        try:
            parsed = datetime.fromisoformat(value_.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value_)
    return str(value_)


def sequence(source: Any, *names: str) -> list[Any]:
    for name in names:
        candidate = value(source, name, None)
        if candidate is not None:
            if isinstance(candidate, Mapping):
                return list(candidate.values())
            if isinstance(candidate, (str, bytes)):
                return [candidate]
            try:
                return list(candidate)
            except TypeError:
                return [candidate]
    return []


_SEVERITY_LABELS = {
    "normal": "정상",
    "ok": "정상",
    "healthy": "정상",
    "attention": "주의",
    "warning": "주의",
    "degraded": "주의",
    "failure": "장애",
    "critical": "장애",
    "down": "장애",
    "unknown": "확인 불가",
    "unavailable": "확인 불가",
    "partial": "확인 불가",
}


def severity_key(source: Any) -> str:
    raw = value(source, "severity", value(source, "overall_status", value(source, "status", "unknown")))
    if isinstance(raw, Enum):
        raw = raw.value
    key = str(raw).strip().casefold().replace(" ", "_")
    aliases = {
        "정상": "normal",
        "주의": "attention",
        "장애": "failure",
        "확인_불가": "unknown",
        "확인불가": "unknown",
    }
    return aliases.get(key, key if key in _SEVERITY_LABELS else "unknown")


def severity_label(source: Any) -> str:
    return _SEVERITY_LABELS.get(severity_key(source), "확인 불가")


@dataclass(slots=True)
class DeviceView:
    source: Any
    ip: str
    alias: str
    hostname: str
    mm_status: str
    active_clients: str
    standby_clients: str
    connection_type: str
    status: str
    status_key: str
    last_seen: str
    issue_reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_source(cls, source: Any) -> "DeviceView":
        reasons = [display(item, "") for item in sequence(source, "issue_reasons", "reasons")]
        return cls(
            source=source,
            ip=display(value(source, "ip"), ""),
            alias=display(value(source, "alias"), ""),
            hostname=display(value(source, "hostname"), ""),
            mm_status=display(value(source, "mm_status")),
            active_clients=display(value(source, "active_clients")),
            standby_clients=display(value(source, "standby_clients")),
            connection_type=display(value(source, "connection_type")),
            status=severity_label(source),
            status_key=severity_key(source),
            last_seen=display(value(source, "last_seen")),
            issue_reasons=[item for item in reasons if item],
        )


@dataclass(slots=True)
class DashboardView:
    source: Any
    status: str
    status_key: str
    devices: list[DeviceView]
    problem_ips: list[str]
    reasons: list[str]
    checked_at: str

    @classmethod
    def from_source(cls, source: Any) -> "DashboardView":
        devices = [DeviceView.from_source(item) for item in sequence(source, "devices", "device_health", "device_healths")]
        missing = object()
        explicit_problem_ips = value(source, "problem_ips", missing)
        if explicit_problem_ips is missing:
            problem_ips = [display(ip, "") for ip in sequence(source, "primary_problem_ips")]
            allow_device_fallback = True
        else:
            if explicit_problem_ips is None:
                problem_ips = []
            else:
                try:
                    problem_ips = [display(ip, "") for ip in explicit_problem_ips]
                except TypeError:
                    problem_ips = [display(explicit_problem_ips, "")]
            allow_device_fallback = False
        primary = value(source, "primary_problem_ip", None)
        if primary and display(primary) not in problem_ips:
            problem_ips.insert(0, display(primary))
        if not problem_ips and allow_device_fallback:
            problem_ips = [device.ip for device in devices if device.status_key not in {"normal", "ok", "healthy"}]
        reasons = [display(reason, "") for reason in sequence(source, "issue_reasons", "reasons", "summary_reasons")]
        summary = display(value(source, "summary", ""), "")
        if summary:
            reasons.insert(0, summary)
        reasons.extend(display(note, "") for note in sequence(source, "notes"))
        for signal in sequence(source, "signals"):
            reason = display(value(signal, "reason", ""), "")
            if reason:
                reasons.append(reason)
        if not reasons:
            for device in devices:
                reasons.extend(device.issue_reasons)
        checked = value(source, "checked_at", value(source, "completed_at", value(source, "last_checked", None)))
        return cls(
            source=source,
            status=severity_label(source),
            status_key=severity_key(source),
            devices=devices,
            problem_ips=[ip for ip in problem_ips if ip],
            reasons=list(dict.fromkeys(reason for reason in reasons if reason)),
            checked_at=display(checked),
        )


def flatten_errors(source: Any) -> list[str]:
    messages: list[str] = []
    for item in sequence(source, "collection_errors", "errors"):
        code = display(value(item, "code", ""), "")
        text = display(value(item, "user_message", value(item, "message", item)), "")
        messages.append(f"{code}: {text}" if code and code not in text else text)
    return [message for message in messages if message]


def safe_raw_output(source: Any) -> str:
    raw = value(source, "raw_output", value(source, "raw_outputs", ""))
    if isinstance(raw, Mapping):
        raw = "\n\n".join(f"[{key}]\n{item}" for key, item in raw.items())
    text = display(raw, "")
    # Aruba command output should never contain secrets, but defensively mask
    # common authentication prompts before presenting it.
    import re

    text = re.sub(r"(?im)^\s*(password|passwd|enable secret)\s*[:=].*$", r"\1: [REDACTED]", text)
    return text
