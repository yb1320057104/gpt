"""mail.com capability-URL mailbox provider.

This adapter is opt-in: only pool lines using ``mail.com://`` or a
``mail.com`` address paired with an HTTP code URL are handled here. Existing
mailbox providers keep their original dispatch path.
"""
from __future__ import annotations

import time
from typing import Any

from curl_cffi import requests as curl_requests

from .mail_otp import _extract_otp_from_text

PROVIDER = "mailcom"


def split_line(value: Any) -> tuple[str, str]:
    text = str(value or "").strip().lstrip("\ufeff")
    for delimiter in ("----", "---"):
        if delimiter in text:
            email, url = (part.strip() for part in text.split(delimiter, 1))
            if "@" in email and url.lower().startswith(("http://", "https://")):
                return email.lower(), url
    return "", ""


def is_line(value: Any) -> bool:
    email, url = split_line(value)
    return bool(email and url and ("mail.com" in email.rsplit("@", 1)[-1].lower() or "/code/" in url.lower()))


def _get(mailbox, *, wait: int = 1, timeout: int = 25, proxy: str | None = None) -> dict:
    url = str(getattr(mailbox, "token", "") or "").strip()
    response = curl_requests.get(url, params={"wait": max(0, min(int(wait), 60))}, timeout=timeout, proxy=proxy)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def fetch_messages(mailbox, *, limit: int = 25, proxy: str | None = None, **kwargs) -> list[dict]:
    payload = _get(mailbox, wait=1, proxy=proxy)
    mail = payload.get("mail") if isinstance(payload.get("mail"), dict) else {}
    code = str(payload.get("code") or "").strip()
    if not code and not mail:
        return []
    return [{
        "id": str(mail.get("id") or "mailcom-current"),
        "subject": str(mail.get("subject") or "verification code"),
        "sender": str(mail.get("sender") or mail.get("from") or "mail.com"),
        "received_at": str(mail.get("date") or ""),
        "body": str(mail.get("body") or code),
        "text": str(mail.get("body") or code),
        "code": code or _extract_otp_from_text(str(mail.get("body") or "")),
        "email": str(payload.get("email") or getattr(mailbox, "email", "")),
    }]


def poll_otp(mailbox, *, timeout: int = 300, issued_after_unix: int = 0,
             excluded_otps=None, proxy: str | None = None,
             subject_keyword: str = "", **kwargs) -> str | None:
    excluded = {str(v) for v in (excluded_otps or set()) if v}
    deadline = time.monotonic() + max(1, int(timeout))
    while time.monotonic() < deadline:
        payload = _get(mailbox, wait=min(10, max(1, int(deadline - time.monotonic()))), proxy=proxy)
        code = str(payload.get("code") or "").strip()
        if code and code not in excluded:
            return code
        time.sleep(1.0)
    return None
