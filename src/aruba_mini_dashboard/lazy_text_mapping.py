from __future__ import annotations

import zlib
from collections.abc import Iterator, Mapping


DEFAULT_COMPRESSION_THRESHOLD_BYTES = 256 * 1024
RAW_OUTPUT_CORRUPTED_MESSAGE = (
    "RAW_OUTPUT_CORRUPTED: 저장된 원본 출력을 읽을 수 없습니다. 다시 점검해 주세요."
)

_SAMPLE_LIMIT_CHARS = 48 * 1024
_SAMPLE_PART_CHARS = _SAMPLE_LIMIT_CHARS // 3
_STREAM_CHUNK_CHARS = 64 * 1024
_MAX_SAMPLE_RATIO = 0.80
_MIN_SAVING_RATIO_PERCENT = 20
_MAX_MINIMUM_SAVING_BYTES = 16 * 1024


class RawOutputCorruptedError(RuntimeError):
    """A sanitized failure raised when an in-memory compressed value is invalid."""


class _CompressedUtf8:
    __slots__ = ("payload", "original_size", "checksum")

    def __init__(self, payload: bytearray, *, original_size: int, checksum: int) -> None:
        self.payload = payload
        self.original_size = original_size
        self.checksum = checksum


def _text_slices(text: str, chunk_chars: int = _STREAM_CHUNK_CHARS) -> Iterator[str]:
    for offset in range(0, len(text), chunk_chars):
        yield text[offset : offset + chunk_chars]


def _utf8_size(text: str) -> int:
    """Return the exact UTF-8 size without allocating one full encoded copy."""

    return sum(len(chunk.encode("utf-8")) for chunk in _text_slices(text))


def _sample_slices(text: str) -> Iterator[str]:
    """Yield representative start/middle/end text totalling at most 48 Ki chars."""

    length = len(text)
    if length <= _SAMPLE_LIMIT_CHARS:
        if text:
            yield text[:]
        return

    part = _SAMPLE_PART_CHARS
    middle_start = max(part, (length - part) // 2)
    end_start = length - part
    yield text[:part]
    yield text[middle_start : middle_start + part]
    yield text[end_start:]


def _sample_compression_ratio(text: str, compression_level: int) -> float | None:
    compressor = zlib.compressobj(compression_level)
    sample_size = 0
    compressed_size = 0
    try:
        for sample in _sample_slices(text):
            encoded = sample.encode("utf-8")
            sample_size += len(encoded)
            compressed_size += len(compressor.compress(encoded))
        compressed_size += len(compressor.flush())
    except UnicodeEncodeError:
        return None
    if sample_size == 0:
        return 1.0
    return compressed_size / sample_size


def _compress_streaming(
    text: str,
    compression_level: int,
) -> tuple[bytearray, int, int] | None:
    """Compress 64 Ki-character chunks without creating a full UTF-8 buffer."""

    compressor = zlib.compressobj(compression_level)
    payload = bytearray()
    original_size = 0
    checksum = 0
    try:
        for chunk in _text_slices(text):
            encoded = chunk.encode("utf-8")
            original_size += len(encoded)
            checksum = zlib.crc32(encoded, checksum)
            payload.extend(compressor.compress(encoded))
        payload.extend(compressor.flush())
    except UnicodeEncodeError:
        return None
    return payload, original_size, checksum


def _compression_is_worthwhile(
    *,
    original_size: int,
    stored_size: int,
    threshold_bytes: int,
) -> bool:
    saving = original_size - stored_size
    minimum_saving = min(
        _MAX_MINIMUM_SAVING_BYTES,
        (threshold_bytes + 15) // 16,
    )
    return (
        saving >= minimum_saving
        and saving * 100 >= original_size * _MIN_SAVING_RATIO_PERCENT
    )


class LazyCompressedTextMapping(Mapping[str, str]):
    """Read-only text mapping that inflates worthwhile large values on access.

    Large values are sampled before compression. Only samples reaching a 0.80
    ratio or better proceed to level-1 streaming compression, and the original
    string reference is retained unless the final payload saves at least 20%
    and the configured minimum number of bytes.
    """

    __slots__ = (
        "_values",
        "_compressed_count",
        "_original_size_bytes",
        "_stored_size_bytes",
    )

    def __init__(
        self,
        source: Mapping[str, str],
        *,
        threshold_bytes: int = DEFAULT_COMPRESSION_THRESHOLD_BYTES,
        compression_level: int = 1,
    ) -> None:
        if threshold_bytes <= 0:
            raise ValueError("threshold_bytes must be positive")
        if not 0 <= compression_level <= 9:
            raise ValueError("compression_level must be between 0 and 9")

        values: dict[str, str | _CompressedUtf8] = {}
        compressed_count = 0
        original_size = 0
        stored_size = 0
        for key, value in source.items():
            if not isinstance(value, str):
                raise TypeError("raw output values must be strings")

            known_size: int | None = None
            # Character count is an exact lower bound for UTF-8 size. Values
            # below that character count are counted in chunks first, so the
            # byte threshold is enforced exactly before sampling/compression.
            if len(value) < threshold_bytes:
                try:
                    known_size = _utf8_size(value)
                except UnicodeEncodeError:
                    values[key] = value
                    continue
                if known_size < threshold_bytes:
                    values[key] = value
                    original_size += known_size
                    stored_size += known_size
                    continue

            sample_ratio = _sample_compression_ratio(value, compression_level)
            if sample_ratio is None:
                values[key] = value
                continue
            if sample_ratio > _MAX_SAMPLE_RATIO:
                if known_size is None:
                    try:
                        known_size = _utf8_size(value)
                    except UnicodeEncodeError:
                        values[key] = value
                        continue
                values[key] = value
                original_size += known_size
                stored_size += known_size
                continue

            compressed = _compress_streaming(value, compression_level)
            if compressed is None:
                values[key] = value
                continue
            payload, value_size, checksum = compressed
            original_size += value_size
            if value_size >= threshold_bytes and _compression_is_worthwhile(
                original_size=value_size,
                stored_size=len(payload),
                threshold_bytes=threshold_bytes,
            ):
                values[key] = _CompressedUtf8(
                    payload,
                    original_size=value_size,
                    checksum=checksum,
                )
                compressed_count += 1
                stored_size += len(payload)
                continue

            values[key] = value
            stored_size += value_size

        self._values = values
        self._compressed_count = compressed_count
        self._original_size_bytes = original_size
        self._stored_size_bytes = stored_size

    def __getitem__(self, key: str) -> str:
        value = self._values[key]
        if isinstance(value, _CompressedUtf8):
            try:
                decompressor = zlib.decompressobj()
                encoded = bytearray(decompressor.decompress(value.payload))
                encoded.extend(decompressor.flush())
                if (
                    not decompressor.eof
                    or bool(decompressor.unused_data)
                    or bool(decompressor.unconsumed_tail)
                    or len(encoded) != value.original_size
                    or zlib.crc32(encoded) != value.checksum
                ):
                    raise RawOutputCorruptedError(RAW_OUTPUT_CORRUPTED_MESSAGE)
                return encoded.decode("utf-8")
            except (zlib.error, UnicodeDecodeError):
                raise RawOutputCorruptedError(RAW_OUTPUT_CORRUPTED_MESSAGE) from None
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: object) -> bool:
        return key in self._values

    @property
    def compressed_count(self) -> int:
        return self._compressed_count

    @property
    def original_size_bytes(self) -> int:
        return self._original_size_bytes

    @property
    def stored_size_bytes(self) -> int:
        return self._stored_size_bytes


def snapshot_raw_outputs(
    source: Mapping[str, str],
    *,
    low_spec_mode: bool,
) -> Mapping[str, str]:
    """Copy raw outputs, using adaptive lazy compression in low-spec mode."""

    if low_spec_mode:
        return LazyCompressedTextMapping(source)
    return dict(source)
