from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit, urlencode, urlunsplit

from .checkout import chatgpt_success_return_url, first_value_by_key
from .config import (
    DEFAULT_STRIPE_PK,
    DEFAULT_TIMEOUT,
    STRIPE_VERSION_BASE,
    STRIPE_VERSION_FULL,
)
from .errors import ProtocolError, ProviderRequiresApproval
from .logging_utils import emit_log, safe_log_text
from .models import CheckoutData, StripeContext
from .providers import provider_redirect_config
from .transport import response_json, stage_http_request

STRIPE_CLIENT_BETAS = (
    "custom_checkout_server_updates_1",
    "custom_checkout_manual_approval_1",
)
STRIPE_ADDITIONAL_ELEMENTS = ("expressCheckout", "payment", "address")


def stripe_key(checkout: CheckoutData) -> str:
    return str(checkout.get("publishable_key") or "").strip() or DEFAULT_STRIPE_PK


def extract_checkout_totals(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    summary = data.get("total_summary") if isinstance(data.get("total_summary"), dict) else {}
    invoice = data.get("invoice") if isinstance(data.get("invoice"), dict) else {}

    def number(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except Exception:
            return None

    return {
        "due": number(summary.get("due", invoice.get("amount_due"))),
        "subtotal": number(summary.get("subtotal", invoice.get("subtotal"))),
        "total": number(summary.get("total", invoice.get("total"))),
        "currency": str(data.get("currency") or invoice.get("currency") or "").lower(),
    }


def expected_amount(payload: Any) -> str:
    due = extract_checkout_totals(payload).get("due")
    if due is None:
        raise ProtocolError(502, "Stripe 未返回可验证的应付金额，不能按 0 元继续提链")
    return str(due)


def checkout_payable_amount(checkout: CheckoutData) -> tuple[int, str]:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    minor_units = due.get("minorUnitsAmount")
    if minor_units in (None, ""):
        minor_units = checkout.get("payable_amount_minor")
    if minor_units in (None, ""):
        from .flows.oaics import openai_checkout_init_payload

        minor_units = extract_checkout_totals(openai_checkout_init_payload(checkout)).get("due")
    if minor_units in (None, ""):
        raise ProtocolError(502, "最终 Checkout 未返回应付金额，无法确认账单是否为 0")
    try:
        amount = int(minor_units)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(502, f"最终 Checkout 应付金额格式无效：{minor_units!r}") from exc
    currency = str(state.get("currency") or checkout.get("currency") or "GBP").upper()
    return amount, currency


def payment_method_types(payload: Any) -> list[str]:
    methods: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            item = value.strip().lower()
            if item and item not in methods:
                methods.append(item)
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict) and value.get("type"):
            add(value["type"])

    data = payload if isinstance(payload, dict) else {}
    sources = (data, data.get("elements_options") if isinstance(data.get("elements_options"), dict) else {})
    for source in sources:
        for key in (
            "payment_method_types",
            "ordered_payment_method_types",
            "ordered_payment_method_types_and_wallets",
            "payment_method_specs",
        ):
            add(source.get(key))
    return methods


def ensure_payment_method_offered(
    payload: dict[str, Any],
    payment_method: str,
    phase: str,
    stage_callback: Any | None = None,
) -> None:
    methods = payment_method_types(payload)
    offered = payment_method in methods
    if stage_callback is not None:
        stage_callback("payment_method_validation", {
            "校验阶段": phase,
            "请求的支付方式": payment_method,
            "Stripe 实际返回的支付方式": methods,
            "校验通过": offered,
            "最终判定": (
                "目标支付方式可用，可以继续确认支付"
                if offered
                else "目标支付方式不可用，无法生成对应的支付跳转链接"
            ),
        })
    if not offered:
        raise ProtocolError(
            409,
            f"{phase} 未提供目标支付方式 {payment_method}；Stripe 实际支付方式={','.join(methods) or '无'}",
        )


def stripe_context(
    init_payload: dict[str, Any],
    checkout: CheckoutData,
    stripe_js_id: str = "",
) -> StripeContext:
    return {
        "stripe_js_id": str(stripe_js_id or uuid.uuid4()),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": str(init_payload.get("config_id") or ""),
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": expected_amount(init_payload),
        "currency": str(init_payload.get("currency") or checkout.get("currency") or "GBP").lower(),
        "locale": str(checkout.get("payment_locale") or "en-GB"),
    }


def cs_stripe_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def openai_stripe_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
    }


def stripe_elements_options_params() -> dict[str, str]:
    return {
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }


def stripe_additional_elements_params(prefix: str) -> dict[str, str]:
    return {
        f"{prefix}[merchant_integration_additional_elements][{index}]": value
        for index, value in enumerate(STRIPE_ADDITIONAL_ELEMENTS)
    }


def cs_elements_client_params(ctx: StripeContext, *, locale_fallback: str = "en") -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": STRIPE_CLIENT_BETAS[0],
        "elements_session_client[client_betas][1]": STRIPE_CLIENT_BETAS[1],
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or ""),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or ""),
        "elements_session_client[locale]": str(ctx.get("locale") or locale_fallback),
        "elements_session_client[is_aggregation_expected]": "false",
    }
    params.update(stripe_elements_options_params())
    return params


def stripe_deferred_intent_params(amount: Any, currency: str, methods: list[str]) -> dict[str, str]:
    normalized_currency = str(currency or "GBP").lower()
    params = {
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": normalized_currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": normalized_currency,
    }
    for index, method in enumerate(methods):
        params[f"deferred_intent[payment_method_types][{index}]"] = method
    return params


def cs_billing_address(billing: dict[str, str], *, country: str | None = None) -> dict[str, str]:
    address = {
        "line1": billing.get("line1", ""),
        "city": billing.get("city", ""),
        "country": country or billing.get("country", ""),
        "postal_code": billing.get("postal_code", ""),
    }
    if billing.get("state"):
        address["state"] = billing["state"]
    return address


def extract_redirect_to_url(payload: Any) -> str:
    """Extract redirect, deep-link, or QR payload from a Stripe next_action."""
    if not isinstance(payload, dict):
        return ""
    next_action = payload.get("next_action")
    if isinstance(next_action, dict):
        redirect = next_action.get("redirect_to_url")
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"]).strip()
        # Local wallets can return a deep link or QR payload instead of an
        # HTTP redirect. Keep the exact provider value so the UI can display
        # and copy it even when it cannot be opened as a web page.
        action_keys = (
            "display_qr_code",
            "pix_display_qr_code",
            "upi_display_qr_code",
            "twint_display_qr_code",
            "kakao_pay_display_qr_code",
            "momo_display_qr_code",
            "momo_handle_redirect_or_display_qr_code",
            "gopay_display_qr_code",
            "gopay_redirect",
            "upi_handle_redirect_or_display_qr_code",
            "mobile_app_redirect",
        )
        value_keys = (
            "data",
            "url",
            "deep_link",
            "mobile_url",
            "qr_code_url",
            "qr_code_data",
            "hosted_voucher_url",
            "hosted_instructions_url",
            "redirect_url",
        )
        for action_key in action_keys:
            action = next_action.get(action_key)
            if isinstance(action, str) and action.strip():
                return action.strip()
            if isinstance(action, dict):
                for value_key in value_keys:
                    value = action.get(value_key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                # UPI and MoMo can wrap their copy value or image/deep-link
                # inside a nested qr_code object.
                nested_qr = action.get("qr_code")
                if isinstance(nested_qr, dict):
                    for value_key in value_keys:
                        value = nested_qr.get(value_key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
    for key in ("setup_intent", "payment_intent", "payment_method_object"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_redirect_to_url(nested)
            if found:
                return found
    return ""


def find_setup_intent(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        setup_intent = payload.get("setup_intent")
        if isinstance(setup_intent, dict):
            return setup_intent
        for value in payload.values():
            found = find_setup_intent(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_setup_intent(value)
            if found:
                return found
    return None


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        if isinstance(payload.get("submission_attempt"), dict):
            return payload["submission_attempt"]
        for value in payload.values():
            found = find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_submission_attempt(value)
            if found:
                return found
    return {}


def stripe_provider_poll(
    stripe: Any,
    checkout: CheckoutData,
    payment_method: str,
    timeout_seconds: float,
    log: Any | None,
    ctx: StripeContext | None = None,
) -> str:
    deadline = time.time() + max(1.0, timeout_seconds)
    last_state = ""
    ctx = ctx or {}
    params = cs_elements_client_params(
        ctx,
        locale_fallback=str(checkout.get("payment_locale") or "en-GB"),
    )
    params.update({"key": stripe_key(checkout), "_stripe_version": STRIPE_VERSION_FULL})
    while time.time() < deadline:
        response = stage_http_request(
            stripe,
            f"Stripe {payment_method} redirect poll",
            "GET",
            f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}",
            log,
            params=params,
            headers=cs_stripe_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response_json(response, f"Stripe {payment_method} poll")
            redirect = extract_redirect_to_url(payload)
            if redirect:
                return redirect
            setup_intent = find_setup_intent(payload)
            if setup_intent:
                redirect = extract_redirect_to_url(setup_intent)
                if redirect:
                    return redirect
            submission = find_submission_attempt(payload)
            last_state = str(submission.get("state") or "")
            if last_state == "requires_approval":
                raise ProviderRequiresApproval(
                    f"Stripe {payment_method} submission requires approval: "
                    f"{safe_log_text(submission, limit=1600)}"
                )
            if last_state == "failed":
                raise ProtocolError(502, f"Stripe {payment_method} submission failed: {safe_log_text(submission)}")
        else:
            last_state = f"http {response.status_code}: {safe_log_text(response.text, limit=1200)}"
            emit_log(log, f"Stripe {payment_method} redirect poll error {last_state}")
        time.sleep(1)
    raise ProtocolError(504, f"{payment_method} redirect poll timeout: state={last_state or '?'}")


def stripe_confirm_return_url(checkout: CheckoutData, hosted_url: str) -> str:
    url = str(hosted_url or "").strip()
    if not url:
        url = f"https://checkout.stripe.com/c/pay/{checkout['cs_id']}"
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    # Stripe hosted Checkout expects the nested destination under
    # success_return_url.  return_url is the confirm field itself and using it
    # as a hosted-page query key loses the post-provider completion target.
    if "success_return_url" in query:
        return url
    query.setdefault("success_return_url", chatgpt_success_return_url(checkout))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def resolve_external_redirect(
    session: Any,
    redirect_url: str,
    preferred_hosts: tuple[str, ...] = ("paypal.com",),
    max_hops: int = 5,
    log: Any | None = None,
) -> str:
    current = str(redirect_url or "").strip()
    preferred = tuple(host.lower().lstrip(".") for host in preferred_hosts)
    for _ in range(max(1, max_hops)):
        if not current:
            return ""
        parsed = urlsplit(current)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return current
        host = (parsed.hostname or "").lower()
        if any(host == item or host.endswith("." + item) for item in preferred):
            return current
        response = None
        # pm-redirects.stripe.com occasionally closes a proxied connection
        # before sending its 302.  Returning that intermediate URL made the
        # UI report a false success, so retry this cheap redirect hop locally.
        for attempt in range(3):
            try:
                response = stage_http_request(
                    session,
                    "Provider redirect hop",
                    "GET",
                    current,
                    log,
                    allow_redirects=False,
                    timeout=DEFAULT_TIMEOUT,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://js.stripe.com/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "cross-site",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                break
            except Exception:
                if attempt >= 2:
                    return current
                time.sleep(0.35 * (attempt + 1))
        if response is None:
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            return current
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


def is_paypal_ba_approval_url(value: str) -> bool:
    """True only for the final PayPal billing-agreement approval URL."""
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").lower()
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return bool(
            (host == "paypal.com" or host.endswith(".paypal.com"))
            and parsed.path.rstrip("/").lower() == "/agreements/approve"
            and str(query.get("ba_token") or "").upper().startswith("BA-")
        )
    except Exception:
        return False


def provider_result(checkout: CheckoutData, payment_method: str, stripe_redirect: str, provider_url: str) -> dict[str, str]:
    provider_config = provider_redirect_config(payment_method)
    url = provider_url or stripe_redirect
    result = {
        "payment_method_id": str(checkout.get("payment_method_id") or ""),
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
    }
    result[provider_config["result_field"]] = url
    return result
