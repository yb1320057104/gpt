from __future__ import annotations

import re
from urllib.parse import urlparse

try:
    from .models import ParsedAccount, ParsedMailbox
except ImportError:  # direct `uvicorn app:app` launch
    from models import ParsedAccount, ParsedMailbox


TOTP_RE = re.compile(r"^[A-Z2-7 ]{16,128}$", re.I)


def is_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_totp(value: str) -> bool:
    compact = re.sub(r"\s+", "", value.strip())
    return bool(TOTP_RE.fullmatch(compact)) and len(compact) >= 16


def parse_account_line(line: str) -> ParsedAccount:
    fields = [part.strip() for part in line.strip().split("----") if part.strip()]
    if len(fields) < 2 or len(fields) > 3:
        raise ValueError("account_fields_must_be_2_or_3")
    email = fields[0].lower()
    if "@" not in email:
        raise ValueError("account_email_invalid")
    account = ParsedAccount(email=email)
    if len(fields) == 2:
        second = fields[1]
        if is_url(second):
            account.access_url = second
        elif is_totp(second):
            account.totp = re.sub(r"\s+", "", second).upper()
        else:
            account.password = second
    else:
        second, third = fields[1:]
        if is_url(third):
            account.password, account.access_url = second, third
        elif is_totp(third):
            account.password, account.totp = second, re.sub(r"\s+", "", third).upper()
        else:
            raise ValueError("third_account_field_must_be_totp_or_access_url")
    return account


def parse_mailbox_line(line: str) -> ParsedMailbox:
    fields = [part.strip() for part in line.strip().split("----") if part.strip()]
    if len(fields) == 2:
        email, access_url = fields
        password = ""
    elif len(fields) == 3:
        email, password, access_url = fields
    else:
        raise ValueError("mailbox_fields_must_be_2_or_3")
    if "@" not in email or not is_url(access_url):
        raise ValueError("mailbox_email_or_access_url_invalid")
    return ParsedMailbox(email=email.lower(), password=password, access_url=access_url)
