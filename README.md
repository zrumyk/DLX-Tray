# DLX Tray

A free desktop translator for Windows. It runs a bundled [DLX](https://github.com/OwO-Network/DLX)
(formerly DeepLX) server in the background and splits large texts into small chunks, so you can
translate whole documents, not just a few sentences.

> **Status:** early development (v0.1.0, project skeleton). The translator UI is not usable yet.
> See the roadmap below.

## Features (planned for v1.0)

- Translate texts of any length: input is split on paragraph, sentence and word boundaries into
  chunks of at most 1500 characters, translated one by one and reassembled.
- Bundled DLX server, started and stopped automatically. No setup.
- Optional fallback to the official DeepL API Free (bring your own key) when DLX fails.
- Automatic source language detection, language swap, formality (official API only).
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
uv run ruff check --fix  # lint
uv run ruff format       # format
uv run pyright           # type check
uv run pre-commit install
```

### Configuration

Copy `.env.example` to `.env` for local development. `.env` is never committed. In the installed
app, the DeepL API key is entered in Settings and stored in Windows Credential Manager.

## Roadmap

| Version | Scope |
|---|---|
| 0.1.0 | Project skeleton, tooling, CI |
| 0.2.0 | Chunker, DLX client and process manager, translation pipeline |
| 0.3.0 | Main window: languages, progress, cancel |
| 0.4.0 | Official DeepL API fallback, error handling |
| 0.5.0 | Settings and themes |
| 0.6.0 | `.txt` / `.docx` / `.pdf` files |
| 0.7.0 | Translation history |
| 0.8.0 | Ukrainian and English UI |
| 0.9.0 | Installer and GitHub Releases |
| 1.0.0 | First stable release |
| 2.0.0 | Tray icon and global hotkey |

## Disclaimer

This is a personal, non-commercial project. It is **not** affiliated with, endorsed by or sponsored
by DeepL SE. "DeepL" is a trademark of DeepL SE. DLX talks to DeepL's free web service in a way
DeepL does not officially support. It may stop working at any time, it is rate limited, and using it
may conflict with DeepL's terms of service. Use at your own risk. For reliable use, configure an
official DeepL API key.

## License

[MIT](LICENSE). DLX is distributed under its own MIT license.
