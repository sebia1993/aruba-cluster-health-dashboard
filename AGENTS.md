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
- Keep SSH, parsers, detectors, correlation, storage, and UI in separate
  modules.
- Tests use sanitized fixtures and local fake SSH services only.

## Release

- `dist/` and build products are never committed.
- A release must pass unit/integration tests, package verification, and local
  EXE smoke checks. Do not claim real Aruba compatibility or a clean
  Python-free Windows validation unless that evidence was actually collected.
- Do not push, tag, or publish without explicit user approval.
