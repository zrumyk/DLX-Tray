"""Verify the privacy filter and packaged binary identity in support reports."""

import hashlib
import json
from pathlib import Path

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
        f"{log_prefix}DLX stopped (pid 13) PRIVATE_PAYLOAD\n"
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
            retries=3,
            events=(),
        )

    result = report()
    assert "Bundled DLX version: v1.3.1" in result
    assert f"Bundled DLX SHA-256: {digest}" in result
    assert "DeepL API key: not set" in result
    assert result.index("(pid 10)") < result.index("(pid 11)") < result.index("(pid 12)")
    assert result.count("[redacted]") == 2
    assert "PRIVATE_PAYLOAD" not in result

    replacement = b"synthetic replacement binary"
    binary.write_bytes(replacement)
    result = report()
    assert "Bundled DLX version: unknown" in result
    assert f"Bundled DLX SHA-256: {hashlib.sha256(replacement).hexdigest()}" in result
