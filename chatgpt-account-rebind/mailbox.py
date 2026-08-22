from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx


CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _extract(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("code", "verification_code", "verificationCode", "otp", "value"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                match = CODE_RE.search(str(value))
                if match:
                    return match.group(1)
        for value in payload.values():
            found = _extract(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _extract(value)
            if found:
                return found
    elif isinstance(payload, str):
        match = CODE_RE.search(payload)
        if match:
            return match.group(1)
    return ""


class MailboxCodeClient:
    def __init__(self, *, proxy: str = "", timeout: float = 20.0) -> None:
        self.http = httpx.Client(proxy=proxy or None, timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def wait_for_code(self, access_url: str, *, since: datetime | None = None, timeout: int = 90, interval: float = 3.0) -> str:
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            response = self.http.get(access_url, params={"wait": min(10, max(1, int(deadline - time.monotonic())))})
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            code = _extract(payload)
            if code:
                return code
            time.sleep(interval)
        raise TimeoutError("verification_code_timeout")
