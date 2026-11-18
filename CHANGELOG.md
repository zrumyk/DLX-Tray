# Changelog

All notable changes to DLX Tray are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- With `DLX_DEEPL_API_KEY` configured, use the official DeepL API for a chunk when DLX is
  rate-limited or unavailable. Translation can also start when bundled DLX fails to launch.
- After DLX returns HTTP 429, use the configured official API during a cooldown that carries
  over to the next translation job.
- Read the official DeepL API key from Windows Credential Manager at startup. A non-blank
  `DLX_DEEPL_API_KEY` environment or development `.env` value takes precedence.
- If a chunk cannot be translated after retries, pause so you can retry it, keep its original
  text and move on, or cancel and keep the result so far.
- The status bar shows which provider is handling translation and warns when DLX reaches its
  rate limit, including the retry delay when the app is waiting to retry DLX.

## [0.3.0] - 2026-11-18

### Added
- Translator layout with side-by-side editors, language pickers and swap, a character and chunk
  counter, a Translate control with Ctrl+Enter, and result copying.
- Translate in the background with chunk progress and results that appear as chunks finish.
  Cancel to stop after the current request and keep completed chunks.
- Auto-detect shows the detected source language after translation and uses it to swap languages
  and texts.
- Start bundled DLX in the background, show its startup status, and stop it when Tray exits.
  A configured external DLX server takes precedence.

### Fixed
- Preserve path prefixes and query parameters in configured DLX server URLs.

### Known issues
- The bundled DLX server can receive HTTP 429 from DeepL. If retries are exhausted, translation
  stops and keeps any completed chunks. Live translation through bundled DLX remains unverified.

## [0.2.0] - 2026-11-18

### Added
- Load preferences from `%APPDATA%\DLX Tray\settings.json` with `DLX_*` development overrides,
  and keep a rotating startup and diagnostics log in `%APPDATA%\DLX Tray\logs`.
- Language, formality, chunk and translation result models with a translation error hierarchy.
- Text chunker that splits any text into pieces of at most 1500 UTF-16 units (1400 by default),
  cutting at paragraph, line, sentence and word boundaries and restoring the layout exactly.
- DLX HTTP client and local process manager with authenticated requests, startup health checks,
  and automatic cleanup on Windows.
- Cancellable translation jobs with progress, partial results, and retry backoff that respects
  `Retry-After` when DLX rate-limits requests.
- Weekly CI watch for new DLX releases, with an issue linking the release notes and asset checksum.
- Property-based tests (hypothesis) and realistic test texts in `tests/fixtures/`.
- 85% coverage gate for `core`, `providers`, `services`, and `fileio`, plus a runtime dependency
  license check and Dependabot.

### Changed
- Depend on `pyside6-essentials` instead of the full `pyside6`: Qt takes ~210 MB instead of
  ~650 MB, which keeps the future installer much smaller.

## [0.1.0] - 2026-11-17

### Added
- Project skeleton: PySide6 app with an empty main window, uv project, ruff, pyright, pytest,
  pre-commit hooks and CI on Windows.
- Script to download the pinned DLX v1.3.1 binary with SHA-256 verification.

[Unreleased]: https://github.com/zrumyk/DLX-Tray/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/zrumyk/DLX-Tray/releases/tag/v0.1.0
