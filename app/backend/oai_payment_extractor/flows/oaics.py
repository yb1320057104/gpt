from __future__ import annotations

import random
import json
import time
from typing import Callable
import uuid
from typing import Any

from ..checkout import (
    chatgpt_success_return_url,
    first_value_by_key,
    merge_checkout_payload,
    openai_checkout_email,
    update_checkout,
)
from ..config import (
    DEFAULT_TIMEOUT,
    OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
    STRIPE_VERSION_BASE,
    STRIPE_VERSION_FULL,
    normalize_payment_method,
    processor_entity_for_country,
)
from ..errors import ProtocolError
from ..models import CheckoutData, ExtractionConfig, StripeContext
from ..providers import provider_redirect_config
from ..stripe_common import (
    cs_stripe_headers,
    ensure_payment_method_offered,
    extract_redirect_to_url,
    openai_stripe_headers,
    is_paypal_ba_approval_url,
    resolve_external_redirect,
    stripe_additional_elements_params,
    stripe_context,
    stripe_deferred_intent_params,
    stripe_key,
)
from ..transport import response_json, stage_http_request


def oaics_fetch_checkout(chatgpt: Any, checkout: CheckoutData, log: Any | None) -> dict[str, Any]:
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    response = stage_http_request(
        chatgpt, "读取 OAICS 自定义支付方式", "GET",
        f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{checkout['cs_id']}",
        log,
        headers={"Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}"},
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"读取 OAICS Checkout 失败: {response.text[:500]}")
    return response_json(response, "读取 OAICS Checkout")


def oaics_custom_method_id(payload: Any, payment_method: str) -> str:
    candidates: list[tuple[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            identifier = str(value.get("id") or "")
            if identifier.startswith("cpmt_"):
                candidates.append((identifier, json.dumps(value, ensure_ascii=False).casefold()))
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    aliases = {
        "gcash": ("gcash", "g_cash"),
        "gopay": ("gopay", "go_pay"),
        "momo": ("momo", "momo_wallet"),
        "kakao_pay": ("kakao_pay", "kakaopay", "kakao"),
        "paypal": ("paypal",),
        "upi": ("upi",),
        "pix": ("pix",),
        "twint": ("twint",),
        "ideal": ("ideal", "i_deal"),
        "blik": ("blik",),
    }
    expected = aliases.get(payment_method.casefold(), (payment_method.casefold(),))
    return next(
        (identifier for identifier, text in candidates if any(alias in text for alias in expected)),
        "",
    )


def extract_oaics_custom_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
    stage_callback: Callable[..., None] | None,
    *,
    update_already_applied: bool = False,
) -> dict[str, str]:
    payment_method = normalize_payment_method(config.payment_method)
    state: dict[str, Any] = {}
    custom_id = ""
    for poll in range(1, 5):
        state = oaics_fetch_checkout(chatgpt, checkout, log)
        custom_id = oaics_custom_method_id(state, payment_method)
        if stage_callback:
            stage_callback("oaics_custom_method", {
                "目标支付方式": payment_method,
                "轮询次数": poll,
                "自定义支付方式ID": custom_id or "尚未返回",
                "OAICS自定义支付方式": custom_payment_method_summaries(state),
            })
        if custom_id:
            break
        time.sleep(0.8 * poll)
    if not custom_id:
        raise ProtocolError(409, f"OAICS 未返回目标支付方式 {payment_method} 的 cpmt_ 通道")

    openai_checkout_taxes(config, chatgpt, checkout, billing, log)
    if config.apply_checkout_update and not update_already_applied:
        if stage_callback:
            stage_callback("checkout_update", {
                "执行状态": "目标自定义支付方式已确认，正在确认前更新 Checkout",
                "目标支付方式": payment_method,
                "更新阶段": "late_promo",
            })
        update_result = update_checkout(config, chatgpt, checkout, log) or {"success": True}
        updated_state = oaics_fetch_checkout(chatgpt, checkout, log)
        merge_checkout_payload(checkout, updated_state)
        if stage_callback:
            stage_callback("checkout_update_result", {
                "执行状态": "确认前 Update 成功",
                "接口success": update_result.get("success", True),
                "目标支付方式": payment_method,
            })
    state = oaics_fetch_checkout(chatgpt, checkout, log)
    # Keep the final server state as the source of truth for the global
    # zero-amount and currency acceptance checks.
    merge_checkout_payload(checkout, state)
    custom_id = oaics_custom_method_id(state, payment_method)
    if not custom_id:
        raise ProtocolError(409, f"OAICS Update 后账号没有目标支付方式 {payment_method} 的资格")
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or config.country),
        str(checkout.get("processor_entity") or ""),
    )
    confirm = stage_http_request(
        chatgpt, f"确认 OAICS {payment_method} 自定义支付方式", "POST",
        "https://chatgpt.com/backend-api/payments/checkout/confirm", log,
        json={"checkout_session_id": checkout["cs_id"], "selected_payment_method_type": custom_id},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": "/backend-api/payments/checkout/confirm",
            "x-openai-target-route": "/backend-api/payments/checkout/confirm",
        }, timeout=DEFAULT_TIMEOUT,
    )
    confirm_payload = response_json(confirm, f"确认 OAICS {payment_method}")
    if confirm.status_code >= 400 or str(confirm_payload.get("status") or "").casefold() != "success":
        raise ProtocolError(confirm.status_code, f"OAICS {payment_method} 自定义支付确认失败: {confirm.text[:500]}")
    started = stage_http_request(
        chatgpt, f"启动 OAICS {payment_method} 自定义支付", "POST",
        "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start", log,
        json={"checkout_session_id": checkout["cs_id"], "custom_payment_method_type_id": custom_id},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": "/backend-api/payments/checkout/custom_payment_method/start",
            "x-openai-target-route": "/backend-api/payments/checkout/custom_payment_method/start",
        }, timeout=DEFAULT_TIMEOUT,
    )
    started_payload = response_json(started, f"启动 OAICS {payment_method}")
    action = started_payload.get("next_action") if isinstance(started_payload.get("next_action"), dict) else {}
    url = str(action.get("url") or action.get("deep_link") or "").strip()
    if started.status_code >= 400 or not url:
        raise ProtocolError(started.status_code, f"OAICS {payment_method} 未返回支付跳转链接: {started.text[:500]}")
    return {"provider_url": url, f"{payment_method}_url": url,
            "custom_payment_method_id": custom_id}


def openai_checkout_init_payload(checkout: CheckoutData) -> dict[str, Any]:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    subtotal = total.get("subtotal") if isinstance(total.get("subtotal"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    return {
        "currency": str(state.get("currency") or checkout.get("currency") or "GBP").lower(),
        "payment_method_types": checkout.get("payment_method_types") or [],
        "custom_payment_methods": checkout.get("custom_payment_methods") or [],
        "total_summary": {
            "due": due.get("minorUnitsAmount"),
            "subtotal": subtotal.get("minorUnitsAmount"),
            "total": due.get("minorUnitsAmount"),
        },
    }


def custom_payment_method_summaries(payload: Any) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    if isinstance(payload, dict):
        custom = payload.get("custom_payment_methods")
        if isinstance(custom, list):
            for item in custom:
                if isinstance(item, dict):
                    summary = {
                        key: str(item.get(key) or "")
                        for key in ("id", "type", "provider", "payment_method_type", "name")
                    }
                    if any(summary.values()) and summary not in summaries:
                        summaries.append(summary)
        for value in payload.values():
            for summary in custom_payment_method_summaries(value):
                if summary not in summaries:
                    summaries.append(summary)
    elif isinstance(payload, list):
        for value in payload:
            for summary in custom_payment_method_summaries(value):
                if summary not in summaries:
                    summaries.append(summary)
    return summaries[:30]


def openai_elements_session(
    stripe: Any,
    config: ExtractionConfig,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
    *,
    reuse_session: bool = False,
) -> dict[str, Any]:
    customer_secret = str(checkout.get("customer_session_client_secret") or "").strip()
    if not customer_secret:
        raise ProtocolError(502, "oaics checkout missing customer_session_client_secret")
    methods = payment_method_types(init_payload)
    ctx["payment_method_types"] = methods
    params = {
        "customer_session_client_secret": customer_secret,
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx["stripe_js_id"],
        "locale": config_locale(config),
        "type": "deferred_intent",
    }
    params.update(stripe_deferred_intent_params(expected_amount(init_payload), config_currency(config), methods))
    if reuse_session and ctx.get("elements_session_id"):
        params["session_id"] = str(ctx["elements_session_id"])
    custom = init_payload.get("custom_payment_methods")
    if isinstance(custom, list):
        for index, item in enumerate(custom):
            custom_id = item.get("id") if isinstance(item, dict) else item
            if custom_id:
                params[f"custom_payment_methods[{index}]"] = str(custom_id)
    response = stage_http_request(
        stripe,
        "Stripe Elements session",
        "GET",
        "https://api.stripe.com/v1/elements/sessions",
        log,
        params=params,
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe Elements session failed: {response.text[:500]}")
    payload = response_json(response, "Stripe Elements session")
    if payload.get("session_id"):
        ctx["elements_session_id"] = str(payload["session_id"])
    if payload.get("config_id"):
        ctx["elements_session_config_id"] = str(payload["config_id"])
        ctx["config_id"] = str(payload["config_id"])
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    customer_session = customer.get("customer_session") if isinstance(customer.get("customer_session"), dict) else {}
    if customer_session.get("customer"):
        ctx["customer_id"] = str(customer_session["customer"])
    return payload


def openai_checkout_taxes(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    body = {
        "checkout_session_id": checkout["cs_id"],
        "checkout_email": openai_checkout_email(checkout) or billing["email"],
        "billing_country": config.country.upper(),
        "billing_name": billing["name"],
        "currency": config_currency(config).lower(),
        "processor_entity": processor,
        "billing_address": {
            "line1": billing["line1"],
            "city": billing["city"],
            "country": config.country.upper(),
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    response = stage_http_request(
        chatgpt,
        "ChatGPT oaics checkout/taxes",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"oaics checkout/taxes failed: {response.text[:500]}")
    payload = response_json(response, "oaics checkout/taxes")
    merge_checkout_payload(checkout, payload)
    return payload


def add_attribution(body: dict[str, str], ctx: StripeContext, prefix: str) -> None:
    values = {
        "client_session_id": ctx.get("stripe_js_id", ""),
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
        "elements_session_id": ctx.get("elements_session_id", ""),
        "elements_session_config_id": ctx.get("elements_session_config_id", ""),
    }
    for key, value in values.items():
        body[f"{prefix}[{key}]"] = str(value)


def openai_confirmation_token(
    stripe: Any,
    config: ExtractionConfig,
    checkout: CheckoutData,
    billing: dict[str, str],
    ctx: StripeContext,
    payment_method: str,
    log: Any | None,
) -> str:
    runtime = OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION
    body = {
        "payment_method_data[type]": payment_method,
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][country]": config.country.upper(),
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[billing_details][phone]": billing["phone"],
        "payment_method_data[payment_user_agent]": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(45000, 85000)),
        "payment_method_data[guid]": ctx["guid"],
        "payment_method_data[muid]": ctx["muid"],
        "payment_method_data[sid]": ctx["sid"],
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": config_currency(config).lower(),
        "client_context[mode]": "subscription",
        "set_as_default_payment_method": "false",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    add_attribution(body, ctx, "payment_method_data[client_attribution_metadata]")
    add_attribution(body, ctx, "client_attribution_metadata")
    body.update(stripe_additional_elements_params("payment_method_data[client_attribution_metadata]"))
    body.update(stripe_additional_elements_params("client_attribution_metadata"))
    for index, method in enumerate(ctx.get("payment_method_types") or []):
        body[f"client_context[payment_method_types][{index}]"] = str(method)
    if ctx.get("customer_id"):
        body["client_context[customer]"] = str(ctx["customer_id"])
    if config.stripe_hcaptcha_token:
        body["payment_method_data[radar_options][hcaptcha_token]"] = config.stripe_hcaptcha_token
    if payment_method == "ideal":
        body["payment_method_data[ideal][bank]"] = config.ideal_bank
    response = stage_http_request(
        stripe,
        "Stripe confirmation token",
        "POST",
        "https://api.stripe.com/v1/confirmation_tokens",
        log,
        data=body,
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe confirmation token failed: {response.text[:500]}")
    token = str(response_json(response, "Stripe confirmation token").get("id") or "")
    if not token.startswith("ctoken_"):
        raise ProtocolError(502, "Stripe confirmation token response missing ctoken_ id")
    return token


def openai_checkout_confirm(
    chatgpt: Any,
    checkout: CheckoutData,
    confirmation_token: str,
    payment_method: str,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/confirm"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    response = stage_http_request(
        chatgpt,
        "ChatGPT oaics checkout/confirm",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={
            "checkout_session_id": checkout["cs_id"],
            "confirm_token": confirmation_token,
            "selected_payment_method_type": payment_method,
        },
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"oaics checkout/confirm failed for {payment_method}: {response.text[:500]}")
    payload = response_json(response, "oaics checkout/confirm")
    if str(payload.get("status") or "").lower() != "success" or not payload.get("client_secret"):
        raise ProtocolError(409, f"oaics checkout/confirm rejected for {payment_method} or missing client_secret")
    return payload


def openai_intent_confirm(
    stripe: Any,
    checkout: CheckoutData,
    confirmation_token: str,
    confirm_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
) -> dict[str, Any]:
    client_secret = str(confirm_payload.get("client_secret") or "").strip()
    if "_secret_" not in client_secret:
        raise ProtocolError(502, "oaics checkout/confirm returned invalid client_secret")
    intent_id = client_secret.split("_secret_", 1)[0]
    if intent_id.startswith("pi_"):
        label = "PaymentIntent"
        endpoint = f"https://api.stripe.com/v1/payment_intents/{intent_id}/confirm"
    elif intent_id.startswith("seti_"):
        label = "SetupIntent"
        endpoint = f"https://api.stripe.com/v1/setup_intents/{intent_id}/confirm"
    else:
        raise ProtocolError(502, "oaics client_secret has unsupported intent type")
    return_url = str(
        confirm_payload.get("confirm_return_url")
        or checkout.get("confirm_return_url")
        or chatgpt_success_return_url(checkout)
    )
    response = stage_http_request(
        stripe,
        f"Stripe {label} confirm",
        "POST",
        endpoint,
        log,
        data={
            "return_url": return_url,
            "confirmation_token": confirmation_token,
            "key": stripe_key(checkout),
            "_stripe_version": STRIPE_VERSION_FULL,
            "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
            "client_attribution_metadata[merchant_integration_source]": "l1",
            "client_secret": client_secret,
        },
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe {label} confirm failed: {response.text[:500]}")
    return response_json(response, f"Stripe {label} confirm")


def extract_oaics_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
    *,
    stage_callback: Callable[..., None] | None = None,
) -> dict[str, str]:
    payment_method = normalize_payment_method(config.payment_method)
    local_late_methods = {"gcash", "gopay", "momo", "kakao_pay", "upi", "pix", "twint", "ideal", "blik"}
    initial_payload = openai_checkout_init_payload(checkout)
    initial_standard_methods = payment_method_types(initial_payload)
    initial_custom_id = oaics_custom_method_id(checkout, payment_method)
    target_initially_offered = payment_method in initial_standard_methods or bool(initial_custom_id)
    update_early = config.apply_checkout_update and (
        not target_initially_offered or payment_method not in local_late_methods
    )
    if update_early:
        if stage_callback:
            stage_callback("checkout_update", {
                "执行状态": "OAICS 初始未提供目标方式，先更新 Checkout 再重新判断" if not target_initially_offered else "按当前支付方式执行早期 Checkout Update",
                "目标支付方式": payment_method,
                "目标账单国家": config.country,
                "更新阶段": "early_update",
            })
        update_result = update_checkout(config, chatgpt, checkout, log) or {"success": True}
        refreshed_state = oaics_fetch_checkout(chatgpt, checkout, log)
        merge_checkout_payload(checkout, refreshed_state)
        if stage_callback:
            stage_callback("checkout_update_result", {
                "执行状态": "更新成功，已重新读取 OAICS 支付通道",
                "接口success": update_result.get("success", True),
                "目标支付方式": payment_method,
                "更新后标准支付方式": payment_method_types(openai_checkout_init_payload(checkout)),
                "更新后自定义支付方式": custom_payment_method_summaries(checkout),
            })
    init_payload = openai_checkout_init_payload(checkout)
    custom_methods = custom_payment_method_summaries(checkout)
    standard_methods = payment_method_types(init_payload)
    if stage_callback:
        stage_callback("oaics_payment_channels", {
            "标准Stripe支付方式": standard_methods,
            "OAICS自定义支付方式": custom_methods,
            "检测到Adyen_MoMo": any(
                item.get("provider", "").casefold() == "adyen"
                and item.get("payment_method_type", "").casefold() == "momo_wallet"
                for item in custom_methods
            ),
        })
    if payment_method not in standard_methods:
        return extract_oaics_custom_provider(
            config, chatgpt, checkout, billing, log, stage_callback,
            update_already_applied=update_early,
        )
    ensure_payment_method_offered(
        init_payload, payment_method, "OAICS Checkout 初始化", stage_callback
    )
    ctx = stripe_context(init_payload, checkout)
    ctx.update(
        {
            "runtime_version": OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
            "guid": str(uuid.uuid4()) + uuid.uuid4().hex[:6],
            "muid": str(uuid.uuid4()) + uuid.uuid4().hex[:6],
            "sid": str(uuid.uuid4()) + uuid.uuid4().hex[:6],
        }
    )
    if stage_callback:
        stage_callback("elements_session", {
            "服务端支付方式": standard_methods,
            "OAICS自定义支付方式": custom_methods,
            "OAICS应付最小金额": expected_amount(init_payload),
            "OAICS币种": str(init_payload.get("currency") or "").upper(),
        })
    openai_elements_session(stripe, config, checkout, init_payload, ctx, log)
    if stage_callback:
        stage_callback("taxes")
    openai_checkout_taxes(config, chatgpt, checkout, billing, log)
    if config.apply_checkout_update and not update_early:
        if stage_callback:
            stage_callback("checkout_update", {
                "执行状态": "目标本地支付方式已初始化，正在确认前更新 Checkout",
                "目标支付方式": payment_method,
                "更新阶段": "late_promo",
            })
        update_result = update_checkout(config, chatgpt, checkout, log) or {"success": True}
        updated_state = oaics_fetch_checkout(chatgpt, checkout, log)
        merge_checkout_payload(checkout, updated_state)
        if stage_callback:
            stage_callback("checkout_update_result", {
                "执行状态": "确认前 Update 成功",
                "接口success": update_result.get("success", True),
                "目标支付方式": payment_method,
            })
    refreshed = openai_checkout_init_payload(checkout)
    ensure_payment_method_offered(
        refreshed, payment_method, "OAICS 税费刷新", stage_callback
    )
    ctx["checkout_amount"] = expected_amount(refreshed)
    ctx["currency"] = config_currency(config).lower()
    session = checkout.get("checkout_session")
    if isinstance(session, dict) and session.get("customer"):
        ctx["customer_id"] = str(session["customer"])
    refreshed_elements = openai_elements_session(
        stripe, config, checkout, refreshed, ctx, log, reuse_session=True
    )
    ensure_payment_method_offered(
        refreshed_elements,
        payment_method,
        "OAICS 刷新后的 Elements Session",
        stage_callback,
    )
    if stage_callback:
        stage_callback("payment_confirmation")
    confirmation_token = openai_confirmation_token(
        stripe, config, checkout, billing, ctx, payment_method, log
    )
    confirm_payload = openai_checkout_confirm(
        chatgpt, checkout, confirmation_token, payment_method, log
    )
    intent_payload = openai_intent_confirm(
        stripe, checkout, confirmation_token, confirm_payload, ctx, log
    )
    stripe_redirect = extract_redirect_to_url(intent_payload)
    if not stripe_redirect:
        raise ProtocolError(502, "oaics intent response missing redirect_to_url")
    provider_config = provider_redirect_config(payment_method)
    if stage_callback:
        stage_callback("redirect_resolution")
    provider_url = resolve_external_redirect(
        stripe,
        stripe_redirect,
        preferred_hosts=provider_config["preferred_hosts"],
        log=log,
    )
    if payment_method == "paypal" and not is_paypal_ba_approval_url(provider_url):
        raise ProtocolError(502, "PayPal BA 链解析失败：Stripe 中转地址未返回 agreements/approve?ba_token=BA- 链接")
    url = provider_url or stripe_redirect
    return {
        "payment_method_id": str(intent_payload.get("payment_method") or ""),
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
        str(provider_config["result_field"]): url,
    }


def payment_method_types(payload: Any) -> list[str]:
    from ..stripe_common import payment_method_types as _payment_method_types

    return _payment_method_types(payload)


def expected_amount(payload: Any) -> str:
    from ..stripe_common import expected_amount as _expected_amount

    return _expected_amount(payload)


def config_currency(config: ExtractionConfig) -> str:
    from ..config import payment_currency

    return payment_currency(config.country, config.payment_method)


def config_locale(config: ExtractionConfig) -> str:
    from ..config import country_config

    return country_config(config.country)[2]
