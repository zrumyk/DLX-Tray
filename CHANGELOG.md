# Changelog

All notable changes to DLX Tray are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Language, formality, chunk and translation result models with a translation error hierarchy.
- Text chunker that splits any text into pieces of at most 1500 UTF-16 units (1400 by default),
  cutting at paragraph, line, sentence and word boundaries and restoring the layout exactly.
- Property-based tests (hypothesis) and realistic test texts in `tests/fixtures/`.
- Test coverage gate (85% of the non-UI code), runtime dependency license check, Dependabot.

## [0.1.0] - 2026-11-17

### Added
- Project skeleton: PySide6 app with an empty main window, uv project, ruff, pyright, pytest,
  pre-commit hooks and CI on Windows.
- Script to download the pinned DLX v1.3.1 binary with SHA-256 verification.

[Unreleased]: https://github.com/zrumyk/DLX-Tray/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zrumyk/DLX-Tray/releases/tag/v0.1.0
