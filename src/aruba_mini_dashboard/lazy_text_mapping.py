from __future__ import annotations

import zlib
from collections.abc import Iterator, Mapping


DEFAULT_COMPRESSION_THRESHOLD_BYTES = 256 * 1024


class _CompressedUtf8:
    __slots__ = ("payload",)

    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class LazyCompressedTextMapping(Mapping[str, str]):
    """Read-only text mapping that inflates large values only when accessed.

    This is intentionally a small, standard-library-only container.  Values
    that do not compress smaller are retained as their original strings, and
    UTF-8 encoding failures fall back to the original value so diagnostics are
    never altered or lost.
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
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                values[key] = value
                continue
            original_size += len(encoded)
            if len(encoded) >= threshold_bytes:
                compressed = zlib.compress(encoded, level=compression_level)
                if len(compressed) < len(encoded):
                    values[key] = _CompressedUtf8(compressed)
                    compressed_count += 1
                    stored_size += len(compressed)
                    continue
            values[key] = value
            stored_size += len(encoded)

        self._values = values
        self._compressed_count = compressed_count
        self._original_size_bytes = original_size
        self._stored_size_bytes = stored_size

    def __getitem__(self, key: str) -> str:
        value = self._values[key]
        if isinstance(value, _CompressedUtf8):
            return zlib.decompress(value.payload).decode("utf-8")
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
    """Copy raw outputs, using lazy level-1 compression in low-spec mode."""

    if low_spec_mode:
        return LazyCompressedTextMapping(source)
    return dict(source)
