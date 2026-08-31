"""Benchmark the v0.7.0 device Model/View pipeline with synthetic inventory.

Only RFC 5737 IPv4 or RFC 3849 IPv6 documentation addresses and reserved
``.example`` hostnames are generated. No network connection is attempted.
"""

from __future__ import annotations

import argparse
import gc
import statistics
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from PySide6.QtCore import Qt

from aruba_mini_dashboard.ui.models import DeviceFilterModel, DeviceTableModel
from aruba_mini_dashboard.ui.models.device_table_model import DeviceTableColumn
from aruba_mini_dashboard.ui.view_models import DeviceView


DEFAULT_SIZES = (250, 500, 1_000, 2_000, 5_000)
MEASUREMENT_KEYS = (
    "initial_load_ms",
    "identical_refresh_ms",
    "sorting_ms",
    "filtering_ms",
)
RESULT_KEYS = ("rows", *MEASUREMENT_KEYS)

_DOCUMENTATION_IPV4_PREFIXES = ("192.0.2", "198.51.100", "203.0.113")
_FIXED_OBSERVED_AT = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def documentation_ip(index: int) -> str:
    """Return a unique documentation-only address for a zero-based row."""

    if index < 0:
        raise ValueError("index must not be negative")
    ipv4_capacity = len(_DOCUMENTATION_IPV4_PREFIXES) * 254
    if index < ipv4_capacity:
        prefix = _DOCUMENTATION_IPV4_PREFIXES[index // 254]
        return f"{prefix}.{index % 254 + 1}"
    # RFC 3849 provides enough non-routable documentation space for the
    # inventories larger than the three RFC 5737 /24 ranges.
    return f"2001:db8::{index - ipv4_capacity + 1:x}"


def build_synthetic_devices(size: int) -> tuple[DeviceView, ...]:
    """Create deterministic, non-identifying DeviceView benchmark rows."""

    count = int(size)
    if count <= 0:
        raise ValueError("size must be greater than zero")
    statuses = (
        ("normal", "정상"),
        ("attention", "주의"),
        ("failure", "장애"),
        ("unknown", "확인 불가"),
    )
    devices: list[DeviceView] = []
    for index in range(count):
        status_key, status = statuses[index % len(statuses)]
        ip = documentation_ip(index)
        active = (index * 37) % 10_000
        standby = (count * 11 - index * 13) % 10_000
        hostname = f"controller-{index:04d}.example"
        devices.append(
            DeviceView(
                source={"ip": ip, "last_seen": _FIXED_OBSERVED_AT},
                ip=ip,
                alias=f"TEST-WLC-{index:04d}",
                hostname=hostname,
                mm_status="Up",
                active_clients=str(active),
                standby_clients=str(standby),
                connection_type="COMMANDER" if index % 2 == 0 else "MEMBER",
                status=status,
                status_key=status_key,
                last_seen="2026-09-01 00:00:00",
                is_registered=True,
                controller_state="up",
                controller_status="Up",
                distribution_state="normal",
                distribution_status="정상",
            )
        )
    return tuple(devices)


def _median_duration_ms(
    operation: Callable[[], Any],
    *,
    repeat: int,
    warmup: int,
) -> float:
    for _ in range(warmup):
        operation()
    durations: list[float] = []
    for _ in range(repeat):
        gc.collect()
        started = perf_counter()
        operation()
        durations.append((perf_counter() - started) * 1_000.0)
    return statistics.median(durations)


def benchmark_size(
    size: int,
    *,
    repeat: int = 5,
    warmup: int = 1,
) -> dict[str, int | float]:
    """Measure one inventory size and return stable, machine-readable keys."""

    count = _positive_int(size, name="size")
    repeats = _positive_int(repeat, name="repeat")
    warmups = _nonnegative_int(warmup, name="warmup")
    devices = build_synthetic_devices(count)

    initial_load_ms = _median_duration_ms(
        lambda: DeviceTableModel(devices).rowCount(),
        repeat=repeats,
        warmup=warmups,
    )

    source = DeviceTableModel(devices)
    identical_refresh_ms = _median_duration_ms(
        lambda: source.set_snapshot(devices),
        repeat=repeats,
        warmup=warmups,
    )

    sort_proxy = DeviceFilterModel(source)
    sort_proxy.rowCount()
    sort_orders = iter(
        Qt.SortOrder.AscendingOrder
        if index % 2 == 0
        else Qt.SortOrder.DescendingOrder
        for index in range(repeats + warmups)
    )

    def sort_devices() -> int:
        sort_proxy.sort(DeviceTableColumn.ACTIVE_CLIENTS, next(sort_orders))
        return sort_proxy.rowCount()

    sorting_ms = _median_duration_ms(
        sort_devices,
        repeat=repeats,
        warmup=warmups,
    )

    filter_proxy = DeviceFilterModel(source)
    filter_proxy.rowCount()
    filter_queries = iter(
        "controller-004" if index % 2 == 0 else "controller-003"
        for index in range(repeats + warmups)
    )

    def filter_devices() -> int:
        filter_proxy.set_search_text(next(filter_queries))
        return filter_proxy.rowCount()

    filtering_ms = _median_duration_ms(
        filter_devices,
        repeat=repeats,
        warmup=warmups,
    )

    return {
        "rows": count,
        "initial_load_ms": initial_load_ms,
        "identical_refresh_ms": identical_refresh_ms,
        "sorting_ms": sorting_ms,
        "filtering_ms": filtering_ms,
    }


def run_benchmarks(
    sizes: Iterable[int] = DEFAULT_SIZES,
    *,
    repeat: int = 5,
    warmup: int = 1,
) -> list[dict[str, int | float]]:
    """Run every requested inventory size in the supplied deterministic order."""

    normalized_sizes = tuple(_positive_int(size, name="size") for size in sizes)
    if not normalized_sizes:
        raise ValueError("at least one size is required")
    return [
        benchmark_size(size, repeat=repeat, warmup=warmup)
        for size in normalized_sizes
    ]


def format_results(results: Sequence[dict[str, int | float]]) -> str:
    """Render benchmark results as a dependency-free plain-text table."""

    headers = (
        "Rows",
        "Initial load (ms)",
        "Identical refresh (ms)",
        "Sorting (ms)",
        "Filtering (ms)",
    )
    rendered_rows = [
        (
            str(int(result["rows"])),
            f'{float(result["initial_load_ms"]):.3f}',
            f'{float(result["identical_refresh_ms"]):.3f}',
            f'{float(result["sorting_ms"]):.3f}',
            f'{float(result["filtering_ms"]):.3f}',
        )
        for result in results
    ]
    widths = [
        max(len(headers[column]), *(len(row[column]) for row in rendered_rows))
        for column in range(len(headers))
    ]

    def render(row: Sequence[str]) -> str:
        return " | ".join(value.rjust(width) for value, width in zip(row, widths, strict=True))

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join((render(headers), separator, *(render(row) for row in rendered_rows)))


def _positive_int(value: int | str, *, name: str = "value") -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _nonnegative_int(value: int | str, *, name: str = "value") -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must not be negative")
    return parsed


def _arg_positive_int(value: str) -> int:
    try:
        return _positive_int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _arg_nonnegative_int(value: str) -> int:
    try:
        return _nonnegative_int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        type=_arg_positive_int,
        nargs="+",
        default=DEFAULT_SIZES,
        help="synthetic inventory sizes (default: 250 500 1000 2000 5000)",
    )
    parser.add_argument(
        "--repeat",
        type=_arg_positive_int,
        default=5,
        help="timed repetitions per operation (default: 5)",
    )
    parser.add_argument(
        "--warmup",
        type=_arg_nonnegative_int,
        default=1,
        help="untimed warmup repetitions per operation (default: 1)",
    )
    args = parser.parse_args(argv)
    results = run_benchmarks(
        args.sizes,
        repeat=args.repeat,
        warmup=args.warmup,
    )
    print(format_results(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
