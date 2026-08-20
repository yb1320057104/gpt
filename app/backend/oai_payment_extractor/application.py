from __future__ import annotations

from dataclasses import replace
import threading
from typing import Any, Callable

from .auth import account_email, account_name, normalize_access_token
from .checkout import check_coupon_eligibility, create_checkout, require_country_currency, update_checkout
from .config import (
    billing_for_country,
    country_config,
    currency_minor_scale,
    normalize_payment_method,
    payment_currency,
    validate_payment_country,
)
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .flows.cs_live import CheckoutSessionUpdated, extract_cs_live_provider
from .flows.oaics import extract_oaics_provider
from .logging_utils import stage_logger
from .models import BillingProfile, ExtractionConfig, PaymentLinkResult
from .transport import DefaultTransportFactory, TransportFactory, reset_request_trace, safe_close, set_proxy_url, set_request_trace
from .stripe_common import checkout_payable_amount


def _find_checkout_value(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", []):
                return value
        for value in payload.values():
            found = _find_checkout_value(value, keys)
            if found not in (None, "", []):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_checkout_value(value, keys)
            if found not in (None, "", []):
                return found
    return None


def _checkout_payment_methods(payload: Any) -> list[str]:
    methods: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"payment_method_types", "ordered_payment_method_types"} and isinstance(value, list):
                methods.extend(str(item) for item in value if str(item).strip())
            else:
                methods.extend(_checkout_payment_methods(value))
    elif isinstance(payload, list):
        for value in payload:
            methods.extend(_checkout_payment_methods(value))
    return list(dict.fromkeys(methods))[:30]


def _normalize_config(config: ExtractionConfig) -> ExtractionConfig:
    token = normalize_access_token(config.access_token)
    if not token:
        raise ConfigurationError("AT is required")
    if not str(config.checkout_proxy or "").strip():
        raise ConfigurationError("checkout proxy is required")
    if config.apply_checkout_update and not str(config.update_proxy or "").strip():
        raise ConfigurationError("update proxy is required")
    country, *_ = country_config(config.country)
    payment_method = normalize_payment_method(config.payment_method)
    validate_payment_country(payment_method, country)
    return replace(
        config,
        access_token=token,
        checkout_proxy=str(config.checkout_proxy).strip(),
        update_proxy=str(config.update_proxy).strip(),
        stripe_hcaptcha_token=str(config.stripe_hcaptcha_token or "").strip(),
        country=country,
        payment_method=payment_method,
    )


def extract_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[..., None] | None = None,
) -> PaymentLinkResult:
    def checkpoint(stage: str, details: dict[str, Any] | None = None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(stage, details or {})

    config = _normalize_config(config)
    log = stage_logger(config.verbose)
    account = account_email(config.access_token)
    billing = billing_for_country(config.country).to_dict()
    # Keep the country-specific address template, while aligning the identity
    # fields with the account used to create the Checkout session.
    if account:
        billing["email"] = account
    name = account_name(config.access_token)
    if name and config.payment_method != "kakao_pay":
        billing["name"] = name
    checkpoint("billing_profile", {
        "billing_country": config.country,
        "billing_currency": country_config(config.country)[1],
        "payment_method_requested": config.payment_method,
    })
    factory = transport_factory or DefaultTransportFactory()
    trace_token = set_request_trace(stage_callback)
    chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = None
    try:
        if config.apply_checkout_update:
            checkpoint("eligibility_check")
            eligibility = check_coupon_eligibility(config, chatgpt, log) or {"state": "eligible"}
            checkpoint("eligibility_confirmed", {
                "trial_eligible": True,
                "资格状态": eligibility.get("state"),
                "优惠活动": eligibility.get("promo_campaign") or eligibility.get("campaign_id") or "plus-1-month-free",
                "资格接口返回字段": sorted(eligibility.keys()),
            })
        else:
            checkpoint("eligibility_skipped", {"trial_eligible": "未检测（未启用 Checkout Update）"})
        checkpoint("checkout")
        checkout = create_checkout(config, chatgpt, log)
        offered_methods = _checkout_payment_methods(checkout)
        checkpoint("checkout_created", {
            "checkout_session_id": str(checkout.get("cs_id") or ""),
            "actual_billing_country": str(_find_checkout_value(checkout, ("billing_country", "country")) or config.country),
            "actual_currency": str(_find_checkout_value(checkout, ("currency",)) or "").upper(),
            "offered_payment_methods": offered_methods,
        })
        checkpoint(f"checkout_kind:{checkout['session_kind']}")
        if config.oaics_only and checkout["session_kind"] == "stripe_checkout":
            raise ConfigurationError("仅 OAICS 模式下检测到 CS Checkout，任务已失败")
        require_country_currency(checkout, config)
        defer_checkout_update = checkout["session_kind"] in {"stripe_checkout", "openai_custom_checkout"}
        if config.apply_checkout_update and not defer_checkout_update:
            checkpoint("checkout_update", {
                "执行状态": "开始更新 Checkout 国家与促销",
                "目标账单国家": config.country,
                "目标支付方式": config.payment_method,
                "目标优惠活动": "plus-1-month-free",
            })
            update_result = update_checkout(config, chatgpt, checkout, log) or {"success": True}
            require_country_currency(checkout, config)
            checkpoint("checkout_update_result", {
                "执行状态": "更新成功",
                "接口success": update_result.get("success", True),
                "实际账单国家": str(_find_checkout_value(checkout, ("billing_country", "country")) or config.country),
                "实际币种": str(_find_checkout_value(checkout, ("currency",)) or "").upper(),
                "优惠状态": _find_checkout_value(update_result, ("state", "status", "promotion_status")) or "接口已接受",
                "优惠活动": _find_checkout_value(update_result, ("promo_campaign_id", "campaign_id")) or "plus-1-month-free",
                "应付最小金额": _find_checkout_value(checkout, ("amount_due", "minorUnitsAmount", "payable_amount_minor")),
                "服务端支付方式": _checkout_payment_methods(checkout),
                "接口返回字段": sorted(update_result.keys()),
            })
        elif config.apply_checkout_update and defer_checkout_update:
            checkpoint("checkout_update", {
                "执行状态": "OAICS 自定义支付方式将按实际账单金额决定是否更新优惠",
                "目标账单国家": config.country,
                "目标支付方式": config.payment_method,
                "目标优惠活动": "plus-1-month-free",
            })
        stripe = factory.stripe(config)
        if checkout["session_kind"] == "stripe_checkout":
            checkpoint("stripe_init")
            checkout_proxies = tuple(dict.fromkeys(
                proxy for proxy in (config.checkout_proxy,) + config.retry_checkout_proxies
                if str(proxy).strip()
            )) or (config.checkout_proxy,)
            provider = {}
            for stripe_attempt, checkout_proxy in enumerate(checkout_proxies, start=1):
                attempt_config = replace(config, checkout_proxy=checkout_proxy)
                if stripe_attempt > 1:
                    checkpoint("stripe_amount_retry", {
                        "reason": "final_bill_nonzero",
                        "retry": stripe_attempt,
                        "proxy_rotated": True,
                    })
                    set_proxy_url(chatgpt, checkout_proxy)
                    safe_close(stripe)
                    stripe = factory.stripe(attempt_config)
                try:
                    provider = extract_cs_live_provider(
                        attempt_config,
                        chatgpt,
                        stripe,
                        checkout,
                        billing,
                        log,
                        stage_callback=checkpoint,
                    )
                except CheckoutSessionUpdated:
                    # The approved pre-Update Stripe attempt is no longer valid
                    # after the merchant-server update. Start a fresh Stripe
                    # flow using the session returned by checkout/update.
                    safe_close(stripe)
                    # Keep apply_checkout_update enabled so the second approval
                    # uses the Update proxy identity. Suppress only the second
                    # merchant update to avoid an update/approval loop.
                    updated_config = attempt_config
                    stripe = factory.stripe(updated_config)
                    checkpoint("stripe_init", {
                        "reason": "checkout_session_updated_after_approval",
                        "checkout_session_id": checkout.get("cs_id"),
                    })
                    provider = extract_cs_live_provider(
                        updated_config,
                        chatgpt,
                        stripe,
                        checkout,
                        billing,
                        log,
                        stage_callback=checkpoint,
                        perform_post_approval_update=False,
                    )
                parsed_amount, parsed_currency = checkout_payable_amount(checkout)
                if parsed_amount == 0:
                    break
                if stripe_attempt == len(checkout_proxies):
                    raise ProtocolError(
                        409,
                        f"最终账单不是 0 元，当前 Stripe 步骤更换全部代理后仍为："
                        f"{parsed_amount} {parsed_currency}（最小货币单位）",
                    )
        elif checkout["session_kind"] == "openai_custom_checkout":
            checkpoint("stripe_init")
            provider = extract_oaics_provider(
                config,
                chatgpt,
                stripe,
                checkout,
                billing,
                log,
                stage_callback=checkpoint,
            )
        else:
            raise ConfigurationError(f"unsupported checkout session: {checkout.get('cs_id')}")
        amount_due_minor, amount_currency = checkout_payable_amount(checkout)
        expected_currency = payment_currency(config.country, config.payment_method).upper()
        if amount_currency != expected_currency:
            raise ProtocolError(
                409,
                f"最终账单币种不符合所选支付方式：要求 {expected_currency}，实际 {amount_currency or '未知'}",
            )
        if amount_due_minor != 0:
            raise ProtocolError(
                409,
                f"最终账单不是 0 元，已停止返回链接：{amount_due_minor} {amount_currency}（最小货币单位）",
            )
        scale = currency_minor_scale(amount_currency)
        amount_due = amount_due_minor / (10**scale)
        provider_field = f"{config.payment_method}_url"
        provider_value = str(provider.get(provider_field) or "").strip()
        if not provider_value:
            raise ProtocolError(
                502,
                f"提链结果缺少所选支付方式 {config.payment_method} 的专属链接字段 {provider_field}",
            )
        result = PaymentLinkResult(
            checkout_session_id=str(checkout["cs_id"]),
            session_kind=str(checkout["session_kind"]),
            payment_method=config.payment_method,
            billing_country=config.country,
            currency=amount_currency,
            amount_due=amount_due,
            amount_due_minor=amount_due_minor,
            billing=BillingProfile(**billing),
            account_email=account,
            payment_method_id=str(provider.get("payment_method_id") or ""),
            stripe_redirect_url=str(provider.get("stripe_redirect_url") or ""),
            provider_url=str(provider.get("provider_url") or provider_value),
            provider_field=provider_field,
            provider_value=provider_value,
        )
        checkpoint("result_summary", {
            "actual_billing_country": config.country,
            "currency": amount_currency,
            "amount_due": amount_due,
            "amount_due_minor": amount_due_minor,
            "is_zero_amount": amount_due_minor == 0,
            "payment_method_extracted": config.payment_method,
            "provider_link_created": bool(provider_value),
            "acceptance_passed": True,
            "acceptance_rule": "支付方式必须匹配且最终应付金额必须为 0",
        })
        checkpoint("completed")
        return result
    finally:
        reset_request_trace(trace_token)
        safe_close(stripe)
        safe_close(chatgpt)
