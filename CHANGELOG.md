# Changelog

All notable changes to DLX Tray are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Choose **DLX only** in Settings to keep translations on the configured DLX provider.
- Added the self-hosted Cloud AI proxy for team translation and term extraction, with personal
  access tokens, usage quotas, and a shared Firestore cache.
- Connect the desktop app to a Cloud AI server in Settings. In Automatic mode, short neutral
  snippets start with DLX and can fall back to Cloud AI and the official DeepL API. Files, longer
  texts, and philological requests use Cloud AI when configured. Cloud AI only skips DLX startup.
- Manage saved glossaries locally and import or export them as UTF-8 CSV or TSV.
- Extract candidate terms from an open file or the editor with Cloud AI. Review, edit, and save
  the equivalents, then translate the same input with the approved terms.
- Choose a stylistic register, glossary, analysis tier, and context hint from the translation
  window. Inspect a segment's alternatives, idioms, grammar notes, and matched terminology.
  History restores notes alongside the saved translation.
- Open **Explain / Notes** after translating a selection to request Cloud AI explanations.
  If deep analysis is unavailable for the token, the popup requests standard notes.
- Attach philological notes as native Word comments when exporting DOCX or PDF translations
  to Word.

### Changed
- The official DeepL provider can translate adjacent uncached segments in a single request
  while preserving their order in the preview and exported file.
- Cloud AI settings have a dedicated section with a masked token, quota check, and hybrid
  threshold. The new controls and notes views are available in Ukrainian.

## [2.0.0] - 2026-11-28

### Added
- The Windows build can optionally start in the notification area when you sign in.
- Select text in another application and press Ctrl+C twice to translate it in a popup.
  Change or disable the selection hotkey in Settings.
- Minimize or close the window to keep translations running in the system tray. The tray icon
  shows provider status and the DLX cooldown. Launching again opens the existing window.
  Choose Exit from the tray or File menu to quit.
- Choose **Official DeepL API only** in Settings to use your DeepL API key for text and files.
  Automatic mode uses DLX with official API fallback when a key is available.
- Reuse completed paragraph and file-segment translations for up to 30 days. Settings can
  clear the translation cache or turn it off.
- Copied diagnostics show the translation worker limit, local cache hits and misses,
  shared results, request pacing, and retry waits alongside provider attempt timings.

### Changed
- See the time remaining before a translation retry in the status bar, and use Cancel to
  keep completed work and stop the retry.
- Reopening a TXT file reuses cached translations for unchanged paragraphs and preserves the
  original paragraph separators in the export.
- Translate with up to two DLX requests at a time while keeping the preview in input order.
  Set Concurrent DLX requests to 1 in Settings to translate sequentially.
- Show paragraph or file-segment progress while streaming completed chunks.
- Use up to 1500 UTF-16 units per DLX request by default. Saved chunk sizes still apply.
- With a chunk size of 750 UTF-16 units or less, send two adjacent chunks that need
  translation in one DLX request when possible.
- Retry temporary translation failures after about two seconds. Server-specified rate-limit
  waits still apply.

### Fixed
- Distinguish DLX authentication failures from ambiguous access denials and report rate limits
  without guessing their cause. After a DLX rate limit, open Settings from the status bar
  to add a DeepL API key for future jobs.
- Upgrade unversioned settings that still use the old 2.5-second request delay to the
  current zero-second default. A 2.5-second delay saved after the upgrade is kept.

### Known issues
- Long-file translation through the live bundled provider remains unverified. Long-file
  translation and export were exercised with a scripted loopback server.

## [1.0.0] - 2026-11-27

### Added
- Open Help → About DLX Tray to see the installed version and the DLX and Qt license notices.
- Copy a Markdown diagnostics report from About or the translation error dialog to share
  version, provider and recent request details without translation text or credentials.
- Follow the illustrated Windows install guide and FAQ for rate limits, SmartScreen and DeepL API keys.

### Changed
- Update bundled DLX for translation without an API key. A short English-to-Ukrainian
  translation passed through the installed Windows app.
- Start the next translation chunk immediately by default. Custom request delays and
  rate-limit waits still apply.

### Fixed
- Bundled DLX passes through the upstream retry delay after a rate limit.
- Bundled DLX keeps cached results for single-text and batch requests distinct and prevents
  queued requests from reaching the upstream service during cooldown.

### Known issues
- Long-file translation through the live bundled provider remains unverified. A short
  installed translation passed through bundled DLX, and long-file translation and UTF-8
  export passed through a scripted loopback server.

## [0.9.0] - 2026-11-19

### Changed
- Pause 2.5 seconds between translation chunks by default. Keep the DLX cooldown across jobs
  after a rate limit and avoid retrying before the server's requested time.

### Added
- Build a standalone Windows app folder with bundled DLX and Ukrainian translations using
  `scripts/build.ps1`.
- Show the DLX Tray icon in Windows and the app version in the executable properties.
- Install DLX Tray for the current Windows user without administrator rights. Setup adds a Start
  menu shortcut and offers a desktop shortcut. Uninstall offers to remove settings, history, and logs.
- Download `DLX-Tray-Setup-0.9.0.exe` from GitHub Releases.

### Fixed
- Build the bundled DLX with the updated DeepL endpoint for anonymous translation.
- Keep user data during an in-place upgrade and require DLX Tray to close before uninstalling.

### Known issues
- Long file translations through the live DLX endpoint remain unverified. File translation was
  tested with a scripted local server.

## [0.8.0] - 2026-11-19

### Added
- Choose English or Ukrainian for the interface in Settings. The choice applies on restart.

### Changed
- The source and target lists show language names in the interface language and follow its
  alphabetical order. The detected source language uses the same localized name.

### Fixed
- File pickers in Ukrainian mode show Ukrainian labels and standard buttons.
- Remove an outdated Settings message that said translation history would arrive in a future update.

### Known issues
- Bundled DLX v1.3.1 can return HTTP 429. Localization was verified with a local scripted server.
  Live file translation remains unverified. See
  [DLX issue #248](https://github.com/OwO-Network/DLX/issues/248).

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

[Unreleased]: https://github.com/zrumyk/DLX-Tray/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/zrumyk/DLX-Tray/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.9.0...v1.0.0
[0.9.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/zrumyk/DLX-Tray/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/zrumyk/DLX-Tray/releases/tag/v0.1.0
