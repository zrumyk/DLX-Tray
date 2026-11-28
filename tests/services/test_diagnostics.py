"""Verify the privacy filter and packaged binary identity in support reports."""

import hashlib
import json
from pathlib import Path
from threading import Event

import pytest

from dlx_tray.core import diagnostics
from dlx_tray.core.diagnostics import DiagnosticSnapshot, DiagnosticTimeline, TrayWait
from dlx_tray.services.diagnostics import markdown_report


def test_report_keeps_safe_rotated_logs_and_only_attributes_matching_dlx(tmp_path: Path) -> None:
    binary = tmp_path / "deeplx.exe"
    dlx_lock = tmp_path / "deeplx.lock.json"
    log_path = tmp_path / "dlx-tray.log"
    original = b"synthetic original binary"
    digest = hashlib.sha256(original).hexdigest()
    binary.write_bytes(original)
    dlx_lock.write_text(
        json.dumps({"version": "v1.3.1", "source_build": {"sha256": digest}}),
        encoding="utf-8",
    )
    log_prefix = "2026-11-27 10:00:00,000 INFO dlx_tray.app "
    log_path.with_suffix(".log.2").write_text(
        f"{log_prefix}DLX stopped (pid 10)\n", encoding="utf-8"
    )
    log_path.with_suffix(".log.1").write_text(
        f"{log_prefix}DLX stopped (pid 11)\n", encoding="utf-8"
    )
    log_path.write_text(
        f"{log_prefix}DLX stopped (pid 12)\n"
        f"{log_prefix}Translation failed for chunk 1 (DlxAuthError)\n"
        f"{log_prefix}Translation failed for chunk 2 (DlxForbiddenError)\n"
        f"{log_prefix}Translation failed for chunk 3 (DlxForbiddenError) PRIVATE_PAYLOAD\n"
        f"{log_prefix}DLX stopped (pid 13) PRIVATE_PAYLOAD\n"
        f"{log_prefix}Cloud request https://test:CLOUD_PASSWORD@cloud.example/v1/me\n"
        f"{log_prefix}Cloud authorization Bearer CLOUD_BEARER_FIXTURE\n"
        "continuation PRIVATE_PAYLOAD\n",
        encoding="utf-8",
    )

    def report() -> str:
        return markdown_report(
            binary=binary,
            dlx_lock=dlx_lock,
            log_path=log_path,
            provider=None,
            dlx_status="ready",
            rate_limited=False,
            has_key=False,
            chunk_size=1400,
            request_delay=0,
            concurrency=None,
            retries=3,
            timeline=DiagnosticSnapshot(),
        )

    result = report()
    assert "Bundled DLX version: v1.3.1" in result
    assert f"Bundled DLX SHA-256: {digest}" in result
    assert "DeepL API key: not set" in result
    assert result.index("(pid 10)") < result.index("(pid 11)") < result.index("(pid 12)")
    assert "Translation failed for chunk 1 (DlxAuthError)" in result
    assert "Translation failed for chunk 2 (DlxForbiddenError)" in result
    assert result.count("[redacted]") == 5
    assert "PRIVATE_PAYLOAD" not in result
    assert "CLOUD_PASSWORD" not in result and "CLOUD_BEARER_FIXTURE" not in result

    replacement = b"synthetic replacement binary"
    binary.write_bytes(replacement)
    result = report()
    assert "Bundled DLX version: unknown" in result
    assert f"Bundled DLX SHA-256: {hashlib.sha256(replacement).hexdigest()}" in result


def test_reports_active_retry_once_before_and_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeline = DiagnosticTimeline()
    now = 100.0
    monkeypatch.setattr(diagnostics, "monotonic", lambda: now)

    def cancel_during_retry(seconds: float, cancel: Event) -> bool:
        nonlocal now
        now += 2.0
        snapshot = timeline.snapshot()
        assert snapshot.events == (TrayWait(seconds, 2.0, "retry", in_progress=True),)
        assert snapshot.retry_waits == 1
        assert snapshot.retry_elapsed == 2.0
        report = markdown_report(
            binary=tmp_path / "no-binary",
            dlx_lock=tmp_path / "no-lock",
            log_path=tmp_path / "no-log",
            provider="dlx",
            dlx_status="external",
            rate_limited=True,
            has_key=False,
            chunk_size=1500,
            request_delay=0,
            concurrency=2,
            retries=1,
            timeline=snapshot,
        )
        assert "Retry waits (session): 1, 2.000 s total elapsed" in report
        assert "Retry wait: 30 s scheduled, 2.000 s elapsed (in progress)" in report
        now += 1.0
        cancel.set()
        return False

    assert not timeline.wait(30, Event(), sleep=cancel_during_retry, kind="retry")
    snapshot = timeline.snapshot()
    assert snapshot.events == (TrayWait(30, 3.0, "retry"),)
    assert snapshot.retry_waits == 1
    assert snapshot.retry_elapsed == 3.0
