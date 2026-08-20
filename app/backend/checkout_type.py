from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from .oai_payment_extractor.config import country_config


CheckoutType = Literal["oaics", "cs"]
CheckoutTypeDetail = Literal[
    "oaics",
    "stripe_cs_live",
    "stripe_cs_test",
    "stripe_checkout",
    "stripe_cs",
]


class CheckoutTypeCheckError(RuntimeError):
    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class CheckoutTypeResult:
    checkout_type: CheckoutType
    checked_at: datetime
    checkout_detail: CheckoutTypeDetail | None = None
    elapsed_ms: int = 0


def checkout_currency(country: str) -> str:
    """Return the currency accepted by the Checkout API for a country."""
    normalized = str(country or "").strip().upper()
    try:
        return str(country_config(normalized)[1]).upper()
    except Exception as exc:
        raise CheckoutTypeCheckError("checkout_country_unsupported") from exc


def checkout_detail_from_result(
    result: dict[str, Any],
) -> CheckoutTypeDetail | None:
    session_id = str(
        result.get("checkoutSessionId") or result.get("checkout_session_id") or ""
    ).strip().casefold()
    if session_id.startswith("oaics_"):
        return "oaics"
    if session_id.startswith("cs_live_"):
        return "stripe_cs_live"
    if session_id.startswith("cs_test_"):
        return "stripe_cs_test"
    if session_id.startswith("cs_"):
        return "stripe_cs"
    session_kind = str(
        result.get("sessionKind") or result.get("session_kind") or ""
    ).strip().casefold()
    if session_kind in {"oaics", "openai_custom_checkout"}:
        return "oaics"
    if session_kind == "stripe_checkout":
        return "stripe_checkout"
    if session_kind == "stripe_cs":
        return "stripe_cs"
    return None


def checkout_type_from_result(result: dict[str, Any]) -> CheckoutType | None:
    session_id = str(
        result.get("checkoutSessionId") or result.get("checkout_session_id") or ""
    ).strip().casefold()
    if session_id.startswith("oaics_"):
        return "oaics"
    if session_id.startswith("cs_"):
        return "cs"
    session_kind = str(
        result.get("sessionKind") or result.get("session_kind") or ""
    ).strip().casefold()
    if session_kind in {"oaics", "openai_custom_checkout"}:
        return "oaics"
    if session_kind in {"stripe_cs", "stripe_checkout"}:
        return "cs"
    return None


def parse_checkout_type_response(payload: Any) -> CheckoutTypeResult:
    if not isinstance(payload, dict):
        raise CheckoutTypeCheckError("checkout_type_response_invalid")

    def find_session_id(value: Any, depth: int = 0) -> str:
        if depth > 8:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("oaics_", "cs_")):
                return text
            return ""
        if isinstance(value, dict):
            for key in ("checkout_session_id", "session_id", "id"):
                found = find_session_id(value.get(key), depth + 1)
                if found:
                    return found
            for nested in value.values():
                found = find_session_id(nested, depth + 1)
                if found:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = find_session_id(nested, depth + 1)
                if found:
                    return found
        return ""

    def find_session_kind(value: Any, depth: int = 0) -> str:
        if depth > 8:
            return ""
        if isinstance(value, dict):
            for key in ("session_kind", "sessionKind"):
                kind = str(value.get(key) or "").strip()
                if kind:
                    return kind
            for nested in value.values():
                found = find_session_kind(nested, depth + 1)
                if found:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = find_session_kind(nested, depth + 1)
                if found:
                    return found
        return ""

    session_id = find_session_id(payload)
    result = {
        "checkout_session_id": session_id,
        "session_kind": find_session_kind(payload),
    }
    checkout_type = checkout_type_from_result(result)
    if checkout_type is None:
        raise CheckoutTypeCheckError("checkout_session_id_missing")
    return CheckoutTypeResult(
        checkout_type,
        datetime.now(timezone.utc),
        checkout_detail_from_result(result),
    )


def check_checkout_type_curl(
    access_token: str,
    *,
    proxy_url: str,
    country: str,
    timeout_seconds: float = 15.0,
) -> CheckoutTypeResult:
    from curl_cffi.requests import Session

    normalized_country = str(country or "").strip().upper()
    if len(normalized_country) != 2:
        raise CheckoutTypeCheckError("checkout_country_missing")
    started = perf_counter()
    currency = checkout_currency(normalized_country)
    session = Session(impersonate="chrome")
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        try:
            response = session.post(
                "https://chatgpt.com/backend-api/payments/checkout",
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {str(access_token).strip()}",
                    "content-type": "application/json",
                    "oai-device-id": str(uuid4()),
                    "oai-language": "en-US",
                },
                json={
                    "entry_point": "all_plans_pricing_modal",
                    "plan_name": "chatgptplusplan",
                    "billing_details": {
                        "country": normalized_country,
                        "currency": currency,
                    },
                    "checkout_ui_mode": "custom",
                },
                allow_redirects=False,
                timeout=max(1.0, min(60.0, float(timeout_seconds))),
            )
        except Exception:
            raise CheckoutTypeCheckError("checkout_type_request_failed") from None
        status = int(response.status_code)
        if not 200 <= status < 300:
            raise CheckoutTypeCheckError(
                "checkout_type_http_failed", http_status=status
            )
        try:
            payload = response.json()
        except Exception:
            raise CheckoutTypeCheckError("checkout_type_response_invalid") from None
        parsed = parse_checkout_type_response(payload)
        return CheckoutTypeResult(
            parsed.checkout_type,
            parsed.checked_at,
            parsed.checkout_detail,
            max(0, round((perf_counter() - started) * 1000)),
        )
    finally:
        session.close()
