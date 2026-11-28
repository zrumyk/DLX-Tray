"""Administer personal Cloud AI tokens through Firestore using ADC."""

from __future__ import annotations

import argparse
import json
import sys

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from .auth import HASH_PATTERN, TokenRecord, TokenStore, utc_now
from .config import CloudSettings
from .errors import CloudError


def _key(value: str) -> str:
    if HASH_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a token hash from the list command")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="issue a token and display it once")
    create.add_argument("label")
    create.add_argument("--daily-limit", type=int, default=250_000)
    create.add_argument("--monthly-limit", type=int, default=3_000_000)
    create.add_argument("--allow-deep-tier", action="store_true")
    create.add_argument("--expires-at", help="ISO 8601 expiration timestamp with a timezone")
    commands.add_parser("list", help="list token hashes and metadata")
    for command in ("revoke", "rotate", "usage"):
        commands.add_parser(command).add_argument("token_hash", type=_key)
    return parser


def _execute(args: argparse.Namespace, store: TokenStore) -> None:
    match args.command:
        case "create":
            record = TokenRecord.model_validate(
                {
                    "label": args.label.strip(),
                    "daily_char_limit": args.daily_limit,
                    "monthly_char_limit": args.monthly_limit,
                    "allow_deep_tier": args.allow_deep_tier,
                    "created_at": utc_now(),
                    "expires_at": args.expires_at,
                }
            )
            if record.expires_at is not None and record.expires_at <= record.created_at:
                raise ValueError("Expiration must be a future timestamp with a timezone.")
            token, key = store.create(record)
            print(json.dumps({"token": token, "token_hash": key}))
        case "list":
            print(
                json.dumps(
                    [
                        {"token_hash": key, **record.model_dump(mode="json")}
                        for key, record in store.list()
                    ]
                )
            )
        case "revoke":
            store.revoke(args.token_hash)
        case "rotate":
            token, key = store.rotate(args.token_hash)
            print(json.dumps({"token": token, "token_hash": key}))
        case "usage":
            record, (daily, monthly) = store.inspect(args.token_hash)
            print(
                json.dumps(
                    {
                        "token_hash": args.token_hash,
                        "label": record.label,
                        "daily_chars_used": daily,
                        "daily_char_limit": record.daily_char_limit,
                        "monthly_chars_used": monthly,
                        "monthly_char_limit": record.monthly_char_limit,
                    }
                )
            )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = CloudSettings()
        pepper = settings.token_pepper.get_secret_value()
        if not pepper:
            raise ValueError("Set DLX_CLOUD_TOKEN_PEPPER before running token administration.")
        db = settings.firestore()
        try:
            _execute(args, TokenStore(db, pepper))
        finally:
            db.close()
    except (CloudError, GoogleAPIError, GoogleAuthError, ValueError):
        print("Token operation failed. Check settings, credentials and arguments.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
