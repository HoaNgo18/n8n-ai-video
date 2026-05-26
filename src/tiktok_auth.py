from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from urllib.parse import urlencode

from dotenv import load_dotenv

from tiktok_publisher import ENV_PATH, exchange_code_for_tokens, refresh_access_token


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"


def require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing")
    return value


def build_auth_url(scopes: str, state: str | None) -> str:
    client_key = require_env("TIKTOK_CLIENT_KEY")
    redirect_uri = require_env("TIKTOK_REDIRECT_URI")
    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state or secrets.token_urlsafe(24),
    }
    return f"{TIKTOK_AUTH_URL}?{urlencode(params)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Utility helpers for TikTok OAuth v2.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_url_parser = subparsers.add_parser("auth-url", help="Print the TikTok OAuth authorization URL.")
    auth_url_parser.add_argument("--scopes", default="user.info.basic,video.publish")
    auth_url_parser.add_argument("--state", default="")

    exchange_parser = subparsers.add_parser("exchange-code", help="Exchange a TikTok auth code for tokens and save them to .env.")
    exchange_parser.add_argument("--code", required=True)

    subparsers.add_parser("refresh", help="Refresh the TikTok access token and save it to .env.")
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH)
    args = parse_args()

    if args.command == "auth-url":
        result = {
            "authorization_url": build_auth_url(args.scopes, args.state or None),
            "scopes": args.scopes,
            "redirect_uri": require_env("TIKTOK_REDIRECT_URI"),
        }
    elif args.command == "exchange-code":
        result = exchange_code_for_tokens(args.code)
    else:
        result = refresh_access_token()

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
