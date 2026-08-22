from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AccountStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    WAITING_CODE = "waiting_code"
    LOGIN_FAILED = "login_failed"
    REBINDING = "rebinding"
    REBOUND = "rebound"
    REBIND_FAILED = "rebind_failed"


class MailboxStatus(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    USED = "used"
    FAILED = "failed"


@dataclass(slots=True)
class ParsedAccount:
    email: str
    password: str = ""
    totp: str = ""
    access_url: str = ""


@dataclass(slots=True)
class ParsedMailbox:
    email: str
    access_url: str
    password: str = ""
