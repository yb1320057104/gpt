from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from client import ApiConfig, ApiLoginError, ChatGPTApiClient


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"


def logger() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger("chatgpt-api")
    log.setLevel(logging.INFO)
    if not log.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        file = logging.FileHandler(LOG_DIR / "chatgpt-api.log", encoding="utf-8")
        file.setFormatter(formatter)
        log.addHandler(stream)
        log.addHandler(file)
    return log


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="ChatGPT pure HTTP login client")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login")
    change = sub.add_parser("change-email")
    change.add_argument("new_email")
    change.add_argument("--endpoint", default=os.getenv("CHATGPT_EMAIL_CHANGE_ENDPOINT", ""))
    change.add_argument("--method", default=os.getenv("CHATGPT_EMAIL_CHANGE_METHOD", "POST"))
    args = parser.parse_args()
    log = logger()
    config = ApiConfig(
        email=os.getenv("CHATGPT_EMAIL", ""),
        password=os.getenv("CHATGPT_PASSWORD", ""),
        totp_secret=os.getenv("CHATGPT_TOTP_SECRET", ""),
        proxy=os.getenv("CHATGPT_PROXY", ""),
        sentinel_token=os.getenv("CHATGPT_SENTINEL_TOKEN", ""),
    )
    if not config.email or not config.password:
        log.error("CHATGPT_EMAIL and CHATGPT_PASSWORD are required")
        return 2
    client = ChatGPTApiClient(config, log)
    try:
        if not config.sentinel_token:
            log.warning("CHATGPT_SENTINEL_TOKEN is empty; auth.openai.com may reject pure HTTP password verification")
        result = client.login()
        if args.command == "change-email":
            result = {"login": result, "emailChange": client.change_email(args.new_email, args.endpoint, args.method)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ApiLoginError, ValueError) as exc:
        log.error("operation failed code=%s", str(exc))
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
