# Changelog

All notable changes to DLX Tray are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Choose English or Ukrainian for the interface in Settings. The choice applies on restart.

### Changed
- The source and target lists show language names in the interface language and follow its
  alphabetical order.

## [0.7.0] - 2026-11-19

### Added
- Save completed text and file translations locally, search history case-insensitively, reopen
  translations in the editors, and delete individual entries or clear the history.
- Turn off history recording or set how many recent translations to keep in Settings.
- Canceled translations are excluded from history.

### Known issues
- Bundled DLX v1.3.1 can return HTTP 429. History was verified with a local scripted server.
  Live file translation remains unverified. See
  [DLX issue #248](https://github.com/OwO-Network/DLX/issues/248).

## [0.6.0] - 2026-11-19

### Added
- Open or drop TXT, DOCX and PDF files, follow translation progress, and save the result.
- Open plain text with encodings such as Windows-1251 and UTF-16 and save as UTF-8.
- Keep text editor contents while translating a file and confirm before discarding a file translation.
- DOCX translation preserves paragraph styles, headings, lists, tables, headers and footers.
- PDF translation reconstructs paragraphs from the text layer and exports to DOCX or UTF-8 TXT.

### Known issues
- File translation was verified with a local scripted server. Live file translation remains
  unverified. Bundled DLX v1.3.1 can return HTTP 429. See
  [DLX issue #248](https://github.com/OwO-Network/DLX/issues/248).

## [0.5.0] - 2026-11-18

### Added
- A Settings dialog for chunk size, request delay, retries, and official DeepL formality.
- Save, remove, or test a masked DeepL API key from Settings. Changes apply to the next translation.
- Choose Light, Dark, or Follow Windows in Settings. Themes change without restarting.
- Save preferences for the upcoming interface language and translation history features.
- Restore the main window's size, position, maximized state, and last selected languages at startup.

### Fixed
- Keep unrelated changes from other running instances when saving preferences.

### Known issues
- Bundled DLX v1.3.1 can return HTTP 429 even on a short request. Live translation through the
  bundled server was deferred for this release. The official DeepL API was verified with a
  configured key. See [DLX issue #248](https://github.com/OwO-Network/DLX/issues/248).

## [0.4.0] - 2026-11-18

### Added
- With `DLX_DEEPL_API_KEY` configured, use the official DeepL API for a chunk when DLX is
  rate-limited or unavailable. Translation can also start when bundled DLX fails to launch.
- After DLX returns HTTP 429, use the configured official API during a cooldown that carries
  over to the next translation job.
- Read the official DeepL API key from Windows Credential Manager at startup when the effective
  `DLX_DEEPL_API_KEY` override from the process environment or development `.env` is missing or blank.
- If a chunk cannot be translated after retries, pause so you can retry it, keep its original
  text and move on, or cancel and keep the result so far.
- The status bar shows which provider is handling translation and warns when DLX reaches its
  rate limit, including the retry delay when the app is waiting to retry DLX.

### Known issues
- Bundled DLX v1.3.1 can return HTTP 429 even on a short request. The cause is unconfirmed.
  See [DLX issue #248](https://github.com/OwO-Network/DLX/issues/248) for upstream reports.
  Translation through the official DeepL API was verified with a configured key.

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

[Unreleased]: https://github.com/zrumyk/DLX-Tray/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/zrumyk/DLX-Tray/releases/tag/v0.1.0
