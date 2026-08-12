from __future__ import annotations

import base64
import os
import random
import zlib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import aruba_mini_dashboard.lazy_text_mapping as lazy_text_mapping
from aruba_mini_dashboard.lazy_text_mapping import (
    RAW_OUTPUT_CORRUPTED_MESSAGE,
    LazyCompressedTextMapping,
    RawOutputCorruptedError,
)
from aruba_mini_dashboard.ui.detail_dialog import DetailDialog
from aruba_mini_dashboard.ui.view_models import (
    iter_safe_raw_output_chunks,
    safe_raw_output,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _deterministic_incompressible_text(size: int = 300_000) -> str:
    random_bytes = random.Random(7).randbytes(size)
    return base64.b85encode(random_bytes).decode("ascii")


def test_compressible_multibyte_output_streams_and_round_trips_exactly() -> None:
    class SliceTrackingText(str):
        requested_slice_lengths: list[int]

        def __new__(cls, value: str):
            instance = super().__new__(cls, value)
            instance.requested_slice_lengths = []
            return instance

        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                self.requested_slice_lengths.append(len(result))
            return result

    raw = SliceTrackingText("한글 Aruba 출력\r\n" * 30_000)
    expected_size = len(str(raw).encode("utf-8"))

    result = LazyCompressedTextMapping({"show test": raw})

    assert result.compressed_count == 1
    assert result.original_size_bytes == expected_size
    assert result.stored_size_bytes < expected_size * 0.8
    assert result["show test"] == raw
    assert raw.requested_slice_lengths
    assert max(raw.requested_slice_lengths) <= 64 * 1024
    assert sum(raw.requested_slice_lengths[:3]) <= 48 * 1024


def test_threshold_uses_utf8_bytes_without_compressing_smaller_text(monkeypatch) -> None:
    real_compressobj = zlib.compressobj
    compressobj_calls = 0

    def counted_compressobj(*args, **kwargs):
        nonlocal compressobj_calls
        compressobj_calls += 1
        return real_compressobj(*args, **kwargs)

    monkeypatch.setattr(lazy_text_mapping.zlib, "compressobj", counted_compressobj)
    below = "x" * (256 * 1024 - 1)
    multibyte_above = "한" * 90_000

    result = LazyCompressedTextMapping(
        {"below": below, "multibyte above": multibyte_above}
    )

    assert result._values["below"] is below
    assert result["multibyte above"] == multibyte_above
    assert result.compressed_count == 1
    # The smaller ASCII value is counted but never sampled or compressed; the
    # qualifying multibyte value uses one sample and one streaming compressor.
    assert compressobj_calls == 2


def test_incompressible_sample_skips_full_compression_and_keeps_original_reference(
    monkeypatch,
) -> None:
    raw = _deterministic_incompressible_text()
    real_compressobj = zlib.compressobj
    compressed_inputs: list[list[int]] = []

    class RecordingCompressor:
        def __init__(self, *args, **kwargs) -> None:
            self._compressor = real_compressobj(*args, **kwargs)
            self.inputs: list[int] = []
            compressed_inputs.append(self.inputs)

        def compress(self, data: bytes) -> bytes:
            self.inputs.append(len(data))
            return self._compressor.compress(data)

        def flush(self) -> bytes:
            return self._compressor.flush()

    monkeypatch.setattr(
        lazy_text_mapping.zlib,
        "compressobj",
        lambda *args, **kwargs: RecordingCompressor(*args, **kwargs),
    )

    result = LazyCompressedTextMapping({"show test": raw})

    assert result.compressed_count == 0
    assert result._values["show test"] is raw
    assert result.original_size_bytes == len(raw)
    assert result.stored_size_bytes == len(raw)
    # Structural benchmark: rejected data performs sample compression only,
    # bounded to 48 Ki characters, rather than compressing the full response.
    assert len(compressed_inputs) == 1
    assert sum(compressed_inputs[0]) <= 48 * 1024


@pytest.mark.parametrize(
    ("original_size", "stored_size", "threshold", "expected"),
    [
        (300_000, 240_000, 256 * 1024, True),
        (300_000, 240_001, 256 * 1024, False),
        (100_000, 80_000, 16 * 1024, True),
        (100_000, 80_000, 512 * 1024, True),
        (100_000, 84_000, 256 * 1024, False),
    ],
)
def test_final_saving_gate_requires_relative_and_absolute_savings(
    original_size: int,
    stored_size: int,
    threshold: int,
    expected: bool,
) -> None:
    assert (
        lazy_text_mapping._compression_is_worthwhile(
            original_size=original_size,
            stored_size=stored_size,
            threshold_bytes=threshold,
        )
        is expected
    )


def test_surrogate_outside_sample_is_retained_without_snapshot_failure() -> None:
    raw = "x" * 100_000 + "\ud800" + "x" * 200_000

    result = LazyCompressedTextMapping({"show test": raw})

    assert result.compressed_count == 0
    assert result["show test"] is raw


def test_corrupt_payload_raises_only_sanitized_stable_failure() -> None:
    result = LazyCompressedTextMapping({"show test": "x" * (300 * 1024)})
    compressed = result._values["show test"]
    assert not isinstance(compressed, str)
    compressed.payload = bytearray(zlib.compress(b"different valid payload", level=1))

    with pytest.raises(RawOutputCorruptedError) as raised:
        result["show test"]

    assert str(raised.value) == RAW_OUTPUT_CORRUPTED_MESSAGE
    assert "zlib" not in str(raised.value).lower()
    assert "different" not in str(raised.value).lower()


@pytest.mark.parametrize("corruption", ["trailing", "truncated"])
def test_compressed_payload_rejects_trailing_or_truncated_data(corruption: str) -> None:
    result = LazyCompressedTextMapping({"show test": "x" * (300 * 1024)})
    compressed = result._values["show test"]
    assert not isinstance(compressed, str)
    if corruption == "trailing":
        compressed.payload.extend(b"unexpected trailing bytes")
    else:
        del compressed.payload[-2:]

    with pytest.raises(RawOutputCorruptedError) as raised:
        result["show test"]

    assert str(raised.value) == RAW_OUTPUT_CORRUPTED_MESSAGE


def test_raw_output_chunks_redact_secrets_and_isolate_one_corrupt_command() -> None:
    mapping = LazyCompressedTextMapping(
        {
            "show first": "first result\n" + "a" * (300 * 1024),
            "show broken": "broken result\n" + "b" * (300 * 1024),
            "show last": "password: never-display\nlast result\n" + "c" * (300 * 1024),
        }
    )
    broken = mapping._values["show broken"]
    assert not isinstance(broken, str)
    broken.payload = bytearray(b"not a zlib stream")

    rendered = "".join(iter_safe_raw_output_chunks({"raw_outputs": mapping}))

    assert "[show first]\nfirst result" in rendered
    assert f"[show broken]\n{RAW_OUTPUT_CORRUPTED_MESSAGE}" in rendered
    assert "[show last]\npassword: [REDACTED]\nlast result" in rendered
    assert "never-display" not in rendered
    assert "not a zlib stream" not in rendered


def test_detail_raw_tab_inserts_each_command_and_continues_after_corruption() -> None:
    _app()
    mapping = LazyCompressedTextMapping(
        {
            "show first": "first result\n" + "a" * (300 * 1024),
            "show broken": "broken result\n" + "b" * (300 * 1024),
            "show last": "password=never-display\nlast result\n" + "c" * (300 * 1024),
        }
    )
    broken = mapping._values["show broken"]
    assert not isinstance(broken, str)
    broken.payload = bytearray(b"invalid")
    dialog = DetailDialog({"ip": "192.0.2.11"}, raw_outputs=mapping)

    dialog.tabs.setCurrentIndex(2)
    rendered = dialog._raw_editor.toPlainText()

    assert "first result" in rendered
    assert RAW_OUTPUT_CORRUPTED_MESSAGE in rendered
    assert "last result" in rendered
    assert "never-display" not in rendered
    assert "[REDACTED]" in rendered
    assert not dialog._raw_editor.isUndoRedoEnabled()
    assert dialog._raw_editor.textCursor().position() == 0
    assert dialog._raw_editor.verticalScrollBar().value() == 0
    dialog.close()


def test_safe_raw_output_preserves_legacy_mapping_format() -> None:
    source = {"raw_outputs": {"show one": "one", "show two": "two"}}

    assert safe_raw_output(source) == "[show one]\none\n\n[show two]\ntwo"
