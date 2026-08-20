from __future__ import annotations

import re
import secrets
import string
from dataclasses import replace
from typing import Callable

from .probe_store import ProxyLease


_SID_PATTERN = re.compile(r"(^|-)sid-([A-Za-z0-9]+)(?=-|$)", re.IGNORECASE)
_LIFETIME_PATTERN = re.compile(r"-t-\d+(?=-|$)", re.IGNORECASE)


def _is_1024proxy(host: str) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    return normalized == "1024proxy.io" or normalized.endswith(".1024proxy.io")


def _session_token(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(1, length)))


def with_registration_sticky_session(
    proxy: ProxyLease,
    *,
    lifetime_minutes: int = 10,
    token_factory: Callable[[int], str] = _session_token,
) -> ProxyLease:
    """Return one task-scoped sticky 1024Proxy lease without mutating storage."""

    if not _is_1024proxy(proxy.host) or not proxy.username or not proxy.password:
        return proxy

    lifetime = min(60, max(1, int(lifetime_minutes)))
    username = proxy.username
    sid_match = _SID_PATTERN.search(username)
    token_length = len(sid_match.group(2)) if sid_match is not None else 8
    token = str(token_factory(token_length))
    if not re.fullmatch(rf"[A-Za-z0-9]{{{token_length}}}", token):
        raise ValueError("sticky proxy session token has an invalid format")

    if sid_match is not None:
        prefix = sid_match.group(1)
        username = (
            username[: sid_match.start()]
            + f"{prefix}sid-{token}"
            + username[sid_match.end() :]
        )
    else:
        lifetime_match = _LIFETIME_PATTERN.search(username)
        insertion = f"-sid-{token}"
        if lifetime_match is None:
            username += insertion
        else:
            username = (
                username[: lifetime_match.start()]
                + insertion
                + username[lifetime_match.start() :]
            )

    if _LIFETIME_PATTERN.search(username):
        username = _LIFETIME_PATTERN.sub(f"-t-{lifetime}", username, count=1)
    else:
        username += f"-t-{lifetime}"

    return replace(proxy, username=username)
