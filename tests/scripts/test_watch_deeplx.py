"""Exercise the release watch with mocked GitHub API responses."""

from unittest.mock import Mock, call

import pytest

from scripts import watch_deeplx

REPO = "owner/project"
ASSET = "deeplx_windows_amd64.exe"
DIGEST = "ab" * 32
RELEASE_URL = "https://github.com/OwO-Network/DLX/releases/tag/v1.10.0"
ISSUE_URL = "https://github.com/owner/project/issues/42"
ISSUES = f"repos/{REPO}/issues"
RELEASE = {
    "tag_name": "v1.10.0",
    "html_url": RELEASE_URL,
    "assets": [{"name": ASSET, "digest": f"sha256:{DIGEST}"}],
}
DETAILS = (
    "<!-- dlx-release-watch:start -->\n"
    f"[DLX v1.10.0 release notes]({RELEASE_URL})\n\n"
    f"- Asset: `{ASSET}`\n"
    f"- SHA-256: `{DIGEST}`\n"
    "<!-- dlx-release-watch:end -->"
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mock = Mock()
    monkeypatch.setattr(watch_deeplx, "github_api", mock)
    return mock


def test_creates_one_issue_across_repeated_checks(api: Mock) -> None:
    api.side_effect = [RELEASE, [[]], {"html_url": ISSUE_URL}]

    watch_deeplx.watch_release(REPO, "v1.9.0", ASSET)

    assert api.call_args == call(
        ISSUES, "-X", "POST", "-f", "title=DLX v1.10.0 available", "-f", f"body={DETAILS}"
    )

    issue = {
        "number": 42,
        "title": "DLX v1.10.0 available",
        "body": f"{DETAILS}\n\nMaintainer notes.",
        "html_url": ISSUE_URL,
    }
    same_title_pr = {**issue, "pull_request": {}}
    api.reset_mock()
    api.side_effect = [RELEASE, [[same_title_pr], [{**issue, "state": "closed"}]]]

    watch_deeplx.watch_release(REPO, "v1.9.0", ASSET)

    assert api.call_args_list == [
        call("repos/OwO-Network/DLX/releases/latest"),
        call(f"{ISSUES}?state=all&per_page=100", "--paginate", "--slurp"),
    ]


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("Maintainer notes.", f"Maintainer notes.\n\n{DETAILS}"),
        (
            f"Before.\n\n{DETAILS.replace(DIGEST, 'ff' * 32)}\n\nAfter.\n",
            f"Before.\n\n{DETAILS}\n\nAfter.\n",
        ),
    ],
)
def test_updates_release_details_without_losing_notes(
    api: Mock, original: str, expected: str
) -> None:
    issue = {"number": 42, "title": "DLX v1.10.0 available", "body": original}
    api.side_effect = [RELEASE, [[issue]], {"html_url": ISSUE_URL}]

    watch_deeplx.watch_release(REPO, "v1.9.0", ASSET)

    assert api.call_args == call(f"{ISSUES}/42", "-X", "PATCH", "-f", f"body={expected}")


@pytest.mark.parametrize("pinned_version", ["v1.10.0", "v2.0.0"])
def test_does_not_report_equal_or_older_releases(api: Mock, pinned_version: str) -> None:
    api.return_value = RELEASE

    watch_deeplx.watch_release(REPO, pinned_version, ASSET)

    api.assert_called_once_with("repos/OwO-Network/DLX/releases/latest")


@pytest.mark.parametrize(
    ("assets", "message"),
    [
        ([], "missing"),
        ([{"name": ASSET, "digest": None}], "no valid SHA-256"),
        ([{"name": ASSET, "digest": "sha256:invalid"}], "no valid SHA-256"),
    ],
)
def test_does_not_report_unverifiable_releases(
    api: Mock, assets: list[dict[str, str | None]], message: str
) -> None:
    api.return_value = {**RELEASE, "assets": assets}

    with pytest.raises(ValueError, match=message):
        watch_deeplx.watch_release(REPO, "v1.9.0", ASSET)

    api.assert_called_once_with("repos/OwO-Network/DLX/releases/latest")
