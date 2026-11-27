# DLX Tray

A free desktop translator for Windows, in development. It is designed to translate long texts
in chunks using a bundled [DLX](https://github.com/OwO-Network/DLX) (formerly DeepLX) server.

> **Windows installer:** v0.9.0 is available on the Releases page. The updated bundled DLX is
> available when you [build from source](#build-from-source) on `main`.

![DLX Tray showing English input and Ukrainian output side by side](.github/screenshots/translation.png)

The screenshot uses an example translation from a local test server.

## Available in v0.9.0

- Install for the current Windows user without administrator rights, with a Start menu shortcut
  and an optional desktop shortcut.
- Choose English or Ukrainian in Settings and restart the app to apply the change. Language
  pickers and the detected language label use the chosen interface language.
- Search saved text and file translations, reopen them in the editors, and delete individual
  entries or clear the history. Set how many recent translations to keep or turn off recording.
- Open or drop a TXT, DOCX or text-based PDF file and follow translation progress across the document.
- Save TXT as UTF-8, preserve headings, lists and tables in DOCX, and export PDF text to DOCX or TXT.
- Split long input on text boundaries into chunks of up to 1500 UTF-16 code units and reassemble
  the translated chunks with their original separators.
- Translate in side-by-side editors using the Translate button or Ctrl+Enter, and copy the result.
- Choose languages or use Auto-detect, then swap the languages and texts.
- See translated chunks as they finish and cancel while keeping the completed result.
- Start and stop the bundled DLX server automatically, with startup status in the main window.
- Fall back to the official DeepL API when DLX is rate-limited or unavailable, using your own
  DeepL API Free or Pro key. The status bar shows which provider is translating.
- Retry, skip, or cancel when translation pauses on a failed chunk.
- Set chunk size, request delay, retries, and official API formality in Settings.
- Save, test, or remove your DeepL API key in Settings.
- Choose Light, Dark, or Follow Windows. Theme changes apply immediately.
- Restore the window's size, position, maximized state, and last languages when the app starts.
- Apply development overrides and write rotating diagnostic logs.

## Install

1. On Windows 10 version 1809 or later or Windows 11 x64, download
   `DLX-Tray-Setup-0.9.0.exe` from the [Releases](https://github.com/zrumyk/DLX-Tray/releases) page.
2. Run Setup for your Windows account. Select the desktop shortcut during setup if you want one.
3. Open **DLX Tray** from Start and wait for **DLX ready**. Enter text, select a target language,
   and choose **Translate** or press **Ctrl+Enter**.

To upgrade, close DLX Tray and run the newer installer over your existing installation. Setup
preserves your settings and translation history.

## FAQ

### What should I do if DLX returns HTTP 429?

HTTP 429 means DLX is rate-limited. Tray waits before retrying and shows the retry delay in the status
bar. You can cancel and keep any translation that already finished. If you save a DeepL API
key, Tray can continue the job through the official API. Check the status bar for the active
provider.

### Why is Windows warning me about the installer?

Microsoft Defender SmartScreen may warn about a downloaded installer with an unfamiliar
reputation. Verify that the download came from the DLX Tray Releases page before opening it.

### Where do I enter my DeepL API key?

A DeepL API Free or Pro key is optional. Open **Settings…**, enter it in **DeepL API key**,
choose **Test key**, then **Save**. The test checks the key without sending translation text.
Tray saves the key in Windows Credential Manager. To remove it, clear the field and save again.

![Settings dialog with the DeepL API key field and Test key button](.github/screenshots/settings.png)

## Build from source

Requirements: Windows 10 version 1809 or later or Windows 11 x64, Python 3.12,
[uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/zrumyk/DLX-Tray.git
cd DLX-Tray
uv sync --all-groups
uv run python scripts/fetch_deeplx.py   # builds the pinned DLX binary into vendor/ on first run
uv run dlx-tray
```

To package the app from source, run `.\scripts\build.ps1` in PowerShell. Open
`dist/DLX Tray/DLX Tray.exe` and keep the entire `dist/DLX Tray` folder together when moving it.

The build on `main` uses the [DLX fork at `177f235`](https://github.com/zrumyk/DLX/commit/177f2355274295a6bee35feaccc27fe06fbd1fa1).
On 27 November 2026, a locally built installer translated **Hello** to **Привіт** in a clean
Windows Sandbox. Tray showed **Provider: DLX** and completed the translation in one request
with no API key or external server configured. Long-file translation and export were tested
separately with a local test server.

Development commands:

```powershell
uv run pytest            # tests (network tests are skipped; run them with -m live)
uv run pytest --cov      # 85% coverage gate for core, providers, services and fileio
uv run --isolated --no-dev --with pip-licenses python scripts/check_licenses.py
uv run ruff check --fix  # lint
uv run ruff format       # format
uv run pyright           # type check
uv run pre-commit install
```

### Configuration

Copy `.env.example` to `.env` for local development. The app loads preferences from
`%APPDATA%\DLX Tray\settings.json` and applies `DLX_*` development overrides. Set
`DLX_DEEPLX_URL` and, if required, `DLX_DEEPLX_TOKEN` to use an external DLX server.

For official DeepL API fallback, save a key in Settings. The app stores it in Windows Credential
Manager and uses changes for the next translation. For development, set `DLX_DEEPL_API_KEY` in
the process environment or local `.env` before starting the app. The process environment takes
precedence over `.env`. If the effective override is missing or blank, the app uses the stored key.

## Roadmap

| Version | Scope |
|---|---|
| 0.1.0 | Project skeleton, tooling, CI (completed) |
| 0.2.0 | Chunker, DLX client and process manager, translation pipeline, local settings and logs (completed) |
| 0.3.0 | Main window: languages, progress, cancel, automatic DLX startup (completed) |
| 0.4.0 | Official DeepL API fallback, error recovery, API key storage (completed) |
| 0.5.0 | Settings, themes, saved window geometry and language selections (completed) |
| 0.6.0 | `.txt` / `.docx` / `.pdf` files (completed) |
| 0.7.0 | Translation history (completed) |
| 0.8.0 | Ukrainian and English UI (completed) |
| 0.9.0 | Installer and GitHub Releases (current) |
| 1.0.0 | First stable release |
| 2.0.0 | System tray icon and global hotkey to translate selected text |

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## Disclaimer

This is a personal, non-commercial project. It is **not** affiliated with, endorsed by or sponsored
by DeepL SE. "DeepL" is a trademark of DeepL SE. DLX talks to DeepL's free web service in a way
DeepL does not officially support. It may stop working at any time, it is rate limited, and using it
may conflict with DeepL's terms of service. Use at your own risk.

## License

[MIT](LICENSE). DLX is distributed under its own MIT license.
