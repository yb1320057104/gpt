from __future__ import annotations

from backend.probe_store import ProxyLease
from backend.proxy_session import with_registration_sticky_session


def fixed_token(length: int) -> str:
    return "S" * length


def test_1024proxy_existing_sid_is_rotated_once_and_lifetime_is_extended() -> None:
    source = ProxyLease(
        "proxy-1",
        "gb.1024proxy.io",
        3000,
        "account-region-GB-sid-OLDtoken-t-5",
        "secret",
        country="GB",
    )

    result = with_registration_sticky_session(
        source,
        lifetime_minutes=10,
        token_factory=fixed_token,
    )

    assert result.username == "account-region-GB-sid-SSSSSSSS-t-10"
    assert result.id == source.id
    assert source.username == "account-region-GB-sid-OLDtoken-t-5"


def test_1024proxy_missing_sid_gets_task_scoped_session_before_lifetime() -> None:
    source = ProxyLease(
        "proxy-2",
        "us.1024proxy.io",
        3000,
        "account-region-GB",
        "secret",
    )

    result = with_registration_sticky_session(
        source,
        token_factory=fixed_token,
    )

    assert result.username == "account-region-GB-sid-SSSSSSSS-t-10"


def test_non_1024proxy_is_not_modified() -> None:
    source = ProxyLease(
        "proxy-3",
        "proxy.example.test",
        8080,
        "account-region-GB-sid-original",
        "secret",
    )

    assert with_registration_sticky_session(source, token_factory=fixed_token) is source
