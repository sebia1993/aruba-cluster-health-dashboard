# Aruba Mini Dashboard Instructions

## Purpose

This repository contains a Windows 11 PySide6 dashboard that monitors Aruba
Mobility Master and Aruba 7240XM cluster state with read-only SSH collection.

## Safety boundaries

- Never send configuration-changing commands to a network device.
- The runtime command allowlist is limited to `show switches`,
  `show lc-cluster load distribution client`,
  `show lc-cluster group-membership`, optional `enable`, and session-only
  `no paging`.
- Do not run live-device tests without explicit user authorization and supplied
  access details.
- Never persist credentials in JSON, SQLite, fixtures, logs, diagnostics, or
  release artifacts. Persistent credentials belong in Windows Credential
  Manager; session-only credentials remain in memory.
- Treat collection, command, and parser failures as unknown/partial collection,
  never as proof that a WLC is down.

## Development

- Use CPython 3.11 and a repository-local `.venv`.
- Run `scripts\run_tests.ps1` before packaging.
- Run `scripts\build.ps1` for the default PyInstaller onedir release.
- Run `scripts\package_release.ps1 -Version <semver>` for a versioned ZIP and
  checksum after source versions and the changelog agree.
- Keep SSH, parsers, detectors, correlation, storage, and UI in separate
  modules.
- Tests use sanitized fixtures and local fake SSH services only.

## Release

- `dist/` and build products are never committed.
- A release must pass unit/integration tests, package verification, and local
  EXE smoke checks. Do not claim real Aruba compatibility or a clean
  Python-free Windows validation unless that evidence was actually collected.
- Keep `THIRD_PARTY_NOTICES.txt` synchronized with the locked runtime and ship
  it in every onedir ZIP. The package verifier must reject unused Qt
  VirtualKeyboard/PDF/QML/Quick/OpenGL artifacts and sensitive runtime files.
- Public packages are onedir-only. Keep PySide6/shiboken6/Paramiko/scp as exact
  reviewed external Python sources, reject every other `.py` file, and prove those modules are
  absent from the embedded PYZ. Ship the Qt/LGPL inventories, license evidence,
  and replacement guide with every package.
- Never overwrite or reuse an existing tag or Release asset. `v0.1.0` is a
  quarantined draft and must not be republished; corrected builds start at
  `v0.1.1`.
- Keep GitHub binary workflows fail-closed until the copyright holder approves
  distribution terms and any needed legal review is complete. After that gate,
  publish only as a Prerelease until actual Aruba and clean Python-free Windows
  11 field evidence exists.
- Do not push, tag, or publish without explicit user approval.
