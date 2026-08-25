"""Deterministic Korea Standard Time helpers for remediation evidence.

Windows does not ship the IANA zoneinfo database used by ``Asia/Seoul``.
Remediation reports are operational evidence, so they must never silently fall
back to UTC.  Korea has used UTC+09:00 without daylight saving since 1988; the
application's supported operating period therefore uses a fixed KST timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


KST = timezone(timedelta(hours=9), "KST")
SUPPORTED_REPORT_TIMEZONES = frozenset({"Asia/Seoul", "KST", "UTC+09:00"})


def report_timezone(name: str = "Asia/Seoul") -> timezone:
    candidate = str(name).strip()
    if candidate not in SUPPORTED_REPORT_TIMEZONES:
        raise ValueError("장애조치 보고서 시간대는 한국 표준시(KST)만 지원합니다.")
    return KST


def as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST)


def format_kst(value: datetime | None, *, clock_only: bool = False) -> str:
    if value is None:
        return "-"
    localized = as_kst(value)
    return localized.strftime("%H:%M:%S KST" if clock_only else "%Y-%m-%d %H:%M:%S KST")
