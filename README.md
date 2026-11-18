# DLX Tray

A free desktop translator for Windows, in development. It is designed to translate long texts
in chunks using a bundled [DLX](https://github.com/OwO-Network/DLX) (formerly DeepLX) server.

> **Status:** v0.3.0 adds the desktop translation window with automatic DLX startup, chunk
> progress, and cancellation.

## Available in v0.3.0

- Split long input on text boundaries into chunks of up to 1500 UTF-16 code units and reassemble
  the translated chunks with their original separators.
- Translate in side-by-side editors using the Translate button or Ctrl+Enter, and copy the result.
- Choose languages or use Auto-detect, then swap the languages and texts.
- See translated chunks as they finish and cancel while keeping the completed result.
- Start and stop the bundled DLX server automatically, with startup status in the main window.
- Load local preferences with development overrides and write rotating diagnostic logs.

## Planned features

- Optional fallback to the official DeepL API Free (bring your own key) when DLX fails.
- Formality control for the official DeepL API.
- Open and save `.txt` and `.docx` files (formatting preserved). `.pdf` input is saved as `.docx` or `.txt`.
- Local translation history with search.
- Ukrainian and English interface, light and dark themes.
- Windows installer.

Later (v2): system tray icon, global hotkey to translate selected text.

## Install

Installers will be published on the [Releases](https://github.com/zrumyk/DLX-Tray/releases) page
starting with v0.9.0.

## Build from source

Requirements: Windows 10/11 x64, Python 3.12, [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/zrumyk/DLX-Tray.git
cd DLX-Tray
uv sync --all-groups
uv run python scripts/fetch_deeplx.py   # downloads the pinned DLX binary into vendor/
uv run dlx-tray
```

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

## Roadmap

| Version | Scope |
|---|---|
| 0.1.0 | Project skeleton, tooling, CI (completed) |
| 0.2.0 | Chunker, DLX client and process manager, translation pipeline, local settings and logs (completed) |
| 0.3.0 | Main window: languages, progress, cancel, automatic DLX startup (current) |
| 0.4.0 | Official DeepL API fallback, error handling |
| 0.5.0 | Settings and themes |
| 0.6.0 | `.txt` / `.docx` / `.pdf` files |
| 0.7.0 | Translation history |
| 0.8.0 | Ukrainian and English UI |
| 0.9.0 | Installer and GitHub Releases |
| 1.0.0 | First stable release |
| 2.0.0 | Tray icon and global hotkey |

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## Disclaimer

This is a personal, non-commercial project. It is **not** affiliated with, endorsed by or sponsored
by DeepL SE. "DeepL" is a trademark of DeepL SE. DLX talks to DeepL's free web service in a way
DeepL does not officially support. It may stop working at any time, it is rate limited, and using it
may conflict with DeepL's terms of service. Use at your own risk. Official DeepL API support is
planned for v0.4.0.

## License

[MIT](LICENSE). DLX is distributed under its own MIT license.
