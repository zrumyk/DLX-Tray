"""Report newer stable DLX releases as GitHub issues."""

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

UPSTREAM = "OwO-Network/DLX"
LOCK_PATH = Path(__file__).with_name("deeplx.lock.json")


def github_api(endpoint: str, *options: str) -> Any:
    response = subprocess.run(
        ["gh", "api", endpoint, *options],
        check=True,
        stdout=subprocess.PIPE,
        encoding="utf-8",
    )
    return json.loads(response.stdout)


def stable_version(tag: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if match is None:
        raise ValueError(f"Expected a stable DLX tag in vX.Y.Z format, got {tag!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def watch_release(repo: str, pinned_version: str, asset_name: str) -> None:
    release = github_api(f"repos/{UPSTREAM}/releases/latest")
    latest_version = release["tag_name"]
    if stable_version(latest_version) <= stable_version(pinned_version):
        print(f"Latest DLX release {latest_version} is not newer than {pinned_version}.")
        return

    asset = next((asset for asset in release["assets"] if asset["name"] == asset_name), None)
    if asset is None:
        raise ValueError(f"DLX {latest_version} is missing the {asset_name} asset")
    digest = asset.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) is None:
        raise ValueError(f"DLX {latest_version} has no valid SHA-256 digest for {asset_name}")

    title = f"DLX {latest_version} available"
    body = (
        f"[DLX {latest_version} release notes]({release['html_url']})\n\n"
        f"- Asset: `{asset_name}`\n"
        f"- SHA-256: `{digest.removeprefix('sha256:').lower()}`\n"
    )
    issues_endpoint = f"repos/{repo}/issues"
    # Look through closed issues too so dismissing a release does not trigger another issue.
    pages = github_api(f"{issues_endpoint}?state=all&per_page=100", "--paginate", "--slurp")
    for page in pages:
        for issue in page:
            if "pull_request" in issue or issue["title"] != title:
                continue
            if (issue["body"] or "").rstrip() != body.rstrip():
                updated = github_api(
                    f"{issues_endpoint}/{issue['number']}", "-X", "PATCH", "-f", f"body={body}"
                )
                print(f"Updated {updated['html_url']}")
            else:
                print(f"Already reported at {issue['html_url']}")
            return

    created = github_api(
        issues_endpoint, "-X", "POST", "-f", f"title={title}", "-f", f"body={body}"
    )
    print(f"Opened {created['html_url']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository to notify (OWNER/REPO)")
    parser.add_argument("--pinned-version", help="Override the locked version for a test run")
    args = parser.parse_args()

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    try:
        watch_release(args.repo, args.pinned_version or lock["version"], lock["asset"])
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
