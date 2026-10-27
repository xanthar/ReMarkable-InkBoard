# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-27

The first public release.

### Added

- **Sketch:** the reMarkable 2's pen read over SSH and drawn live in a browser page on
  `127.0.0.1`, the line width following pressure; a Vertical and a Horizontal view for portrait and
  landscape; reconnecting by itself after the tablet sleeps; a stroke counter and Clear.
- **Screen:** the tablet's own screen a moment after the pen lifts, read-only from xochitl's
  memory over the same SSH login, with a firmware gate on the tablet (3.24 and 3.29) and a size and
  opacity check before any frame is used; Refresh; the toolbar recognised from its icons so Copy
  menu can leave it out of the image.
- **Image export:** Copy image (PNG on the clipboard for an agent chat) and Save PNG, at the
  tablet's screen size, for either view.
- **USB or Wi-Fi:** the cable when it is in, the tablet's Wi-Fi when it is not, switching by
  itself within seconds; the Wi-Fi address remembered from the tablet's own report, listed in
  `INKBOARD_WIFI_ADDRESSES`, or found by a quiet, opt-in search for the tablet's MAC; every Wi-Fi
  connection checks the tablet's host key before the login key is offered.
- **Commands:** `serve` (also replaying a recording), `record`, `probe` (finds the pen device,
  record size and orientation) and `demo`.
- **Settings** from `INKBOARD_*` environment variables or a local `.env`, with `.env.example`.
- **Tablet backup:** `scripts/tablet_backup.py` copies and verifies the whole tablet storage and a
  file copy of `/home` and `/etc`, with a one-file restore test.
- **Documentation:** README, architecture, a tablet runbook (D0–D8) with a troubleshooting table,
  three ADRs, SECURITY, CONTRIBUTING, Code of Conduct, and a Wi-Fi-only SSH unit file in
  `contrib/`.
- **Quality gates in CI:** Ruff, mypy, pytest with a 98% coverage floor on Linux and Windows,
  Bandit, pip-audit with a hashed lock audit, osv-scanner, gitleaks, CodeQL and Dependabot.

[Unreleased]: https://github.com/xanthar/ReMarkable-InkBoard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/xanthar/ReMarkable-InkBoard/releases/tag/v0.1.0
