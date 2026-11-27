"""Build a Markdown support report from bounded, privacy-filtered data."""

import hashlib
import json
import platform
import re
from collections import deque
from collections.abc import Sequence
from pathlib import Path

from dlx_tray import __version__
from dlx_tray._file_lock import file_lock
from dlx_tray.core.diagnostics import DiagnosticProvider, DLXConnection, ProviderAttempt, TrayWait

_LOG_LINE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (?:DEBUG|INFO|WARNING|ERROR)) "
    r"dlx_tray(?:\.[a-z_]+)* (.*)",
    re.ASCII,
)
_SAFE_MESSAGES = re.compile(
    r"Starting DLX Tray \d+\.\d+\.\d+"
    r"|DLX started on http://127\.0\.0\.1:\d+ \(pid \d+, attempt \d+\)"
    r"|DLX stopped \(pid \d+\)"
    r"|DLX exited with code -?\d+ during startup \(attempt \d+\)"
    r"|DLX answered HTTP \d{3}"
    r"|DLX request timed out after \d+ s"
    r"|DeepL translation failed: (?:TooManyRequestsException|AuthorizationException|"
    r"QuotaExceededException|ConnectionException|DeepLException) \(HTTP (?:\d{3}|None)\)"
    r"|Translation provider dlx failed \((?:RateLimitedError|ProviderUnavailableError|AuthError)\)"
    r", falling back to deepl"
    r"|Translation failed for chunk \d+ \((?:RateLimitedError|ProviderUnavailableError|"
    r"AuthError|TextTooLongError|TranslationError)\)"
    r"|DeepL returned a malformed translation response"
    r"|DeepL returned invalid results for \d+ texts"
    r"|Could not read the DeepL API key from Windows Credential Manager",
    re.ASCII,
)


def _safe_log_line(line: str) -> str:
    match = _LOG_LINE.fullmatch(line.rstrip("\r\n"))
    if match is None:
        return "[redacted]"
    timestamp, message = match.groups()
    if not _SAFE_MESSAGES.fullmatch(message):
        return f"{timestamp} [redacted]"
    return f"{timestamp} {message}"


def _log_tail(log_path: Path) -> deque[str]:
    recent = deque[str](maxlen=100)
    try:
        with file_lock(log_path.with_suffix(".lock")):
            for path in (log_path.with_suffix(".log.2"), log_path.with_suffix(".log.1"), log_path):
                try:
                    with path.open(encoding="utf-8", errors="replace") as stream:
                        recent.extend(_safe_log_line(line) for line in stream)
                except FileNotFoundError:
                    continue
    except OSError:
        recent.append("[log unavailable]")
    return recent


def _bundled_dlx(binary: Path, lock_path: Path) -> tuple[str, str]:
    try:
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return "unknown", "unknown"

    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        build = lock.get("source_build") or lock.get("patched_build") or lock
        version = lock.get("version")
        if (
            build.get("sha256") == digest
            and isinstance(version, str)
            and re.fullmatch(r"v\d+\.\d+\.\d+", version, re.ASCII)
        ):
            return version, digest
    except (OSError, ValueError, AttributeError):
        pass
    return "unknown", digest


def markdown_report(
    *,
    binary: Path,
    dlx_lock: Path,
    log_path: Path,
    provider: DiagnosticProvider | None,
    dlx_status: DLXConnection,
    rate_limited: bool,
    has_key: bool,
    chunk_size: int,
    request_delay: float,
    retries: int,
    events: Sequence[ProviderAttempt | TrayWait],
) -> str:
    dlx_version, dlx_digest = _bundled_dlx(binary, dlx_lock)
    windows = platform.win32_ver()[1] or "unknown"
    provider_name = {"dlx": "DLX", "deepl": "DeepL API"}.get(provider or "", "none yet")
    lines = [
        "## DLX Tray diagnostics",
        "",
        f"- App version: {__version__}",
        f"- Bundled DLX version: {dlx_version}",
        f"- Bundled DLX SHA-256: {dlx_digest}",
        f"- Windows version: {windows}",
        f"- Python version: {platform.python_version()}",
        f"- Active provider: {provider_name}",
        f"- DLX connection: {dlx_status}",
        f"- DLX rate-limited: {'yes' if rate_limited else 'no'}",
        f"- DeepL API key: {'set' if has_key else 'not set'}",
        f"- Chunk size: {chunk_size} UTF-16 units",
        f"- Request delay: {request_delay:g} s",
        f"- Retries: {retries}",
        "",
        "### Recent chunk attempts and waits",
        "",
    ]
    for event in events:
        if isinstance(event, TrayWait):
            lines.append(f"- Tray wait: {event.seconds:g} s scheduled")
            continue
        retry_after = "unknown" if event.retry_after is None else f"{event.retry_after:g} s"
        status = "unknown" if event.status is None else str(event.status)
        lines.append(
            f"- {event.provider}, {event.elapsed:.3f} s, HTTP {status}, "
            f"Retry-After {retry_after}, cache {event.cache or 'unknown'}"
        )
    if not events:
        lines.append("No attempts recorded.")

    lines.extend(("", "### Latest log lines", ""))
    lines.extend(f"    {line}" for line in _log_tail(log_path) or ("No log lines available.",))
    return "\n".join(lines) + "\n"
