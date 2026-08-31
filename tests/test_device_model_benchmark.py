from __future__ import annotations

import math

from scripts.benchmark_device_models import (
    DEFAULT_SIZES,
    MEASUREMENT_KEYS,
    RESULT_KEYS,
    build_synthetic_devices,
    format_results,
    run_benchmarks,
)


def test_default_benchmark_sizes_keys_and_5000_row_smoke() -> None:
    assert DEFAULT_SIZES == (250, 500, 1_000, 2_000, 5_000)
    assert MEASUREMENT_KEYS == (
        "initial_load_ms",
        "identical_refresh_ms",
        "sorting_ms",
        "filtering_ms",
    )

    results = run_benchmarks(DEFAULT_SIZES, repeat=1, warmup=0)

    assert [result["rows"] for result in results] == list(DEFAULT_SIZES)
    assert all(tuple(result) == RESULT_KEYS for result in results)
    for result in results:
        for key in MEASUREMENT_KEYS:
            assert math.isfinite(float(result[key]))
            assert float(result[key]) >= 0.0
    assert results[-1]["rows"] == 5_000

    table = format_results(results)
    assert "Initial load (ms)" in table
    assert "Identical refresh (ms)" in table
    assert "5000" in table


def test_synthetic_inventory_uses_documentation_addresses_and_virtual_names() -> None:
    devices = build_synthetic_devices(5_000)

    assert len(devices) == 5_000
    assert devices[0].ip == "192.0.2.1"
    assert devices[253].ip == "192.0.2.254"
    assert devices[254].ip == "198.51.100.1"
    assert devices[508].ip == "203.0.113.1"
    assert devices[762].ip == "2001:db8::1"
    assert len({device.ip for device in devices}) == 5_000
    assert all(device.hostname.endswith(".example") for device in devices)
    assert all(device.alias.startswith("TEST-WLC-") for device in devices)
