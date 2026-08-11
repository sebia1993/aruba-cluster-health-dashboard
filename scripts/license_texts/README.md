# Supplemental third-party license sources

These are verbatim license texts needed when the installed Windows wheel does
not contain the complete open-source license evidence. They are inputs to
`scripts/collect_third_party_licenses.py`; they do not grant or select a license
for Aruba Mini Dashboard itself.

- `PySide6-*.txt` came from the official Qt for Python `v6.11.0` source tag,
  commit `04cd59c10681242e387d125cfe5269902962ded1`, under `LICENSES/`.
- `pyserial-3.5-LICENSE.txt` came from `LICENSE.txt` in the PyPI pyserial 3.5
  source archive whose SHA-256 is
  `3c77e014170dfffbd816e6ffc205e9842efb10be9f58ec16d3e8675b4925cddb`.

The collector pins the SHA-256 of every file after UTF-8/LF normalization and
fails closed if a source text is missing or changed. Update the source
description, pinned hash, generated notice, and focused tests together when a
dependency version changes.
