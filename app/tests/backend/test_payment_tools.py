from __future__ import annotations

import base64
import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.oai_iprocket_chain_bridge as bridge_module
from backend.main import create_app
from backend.mongo_manager import MongoManager
from backend.oai_payment_extractor import application
from backend.oai_payment_extractor.config import payment_currency
from backend.oai_payment_extractor.checkout import update_checkout
from backend.oai_payment_extractor.models import ExtractionConfig, PaymentLinkResult
from backend.oai_payment_extractor.errors import NetworkError, ProtocolError
from backend.oai_payment_extractor.stripe_common import checkout_payable_amount, extract_redirect_to_url
from backend.oai_payment_extractor.stripe_common import stripe_confirm_return_url
from backend.oai_payment_extractor.flows.cs_live import stripe_wallet_pre_confirm
import backend.oai_payment_extractor.flows.cs_live as cs_live_module
from backend.oai_payment_extractor.flows.oaics import oaics_custom_method_id
from backend.oai_payment_extractor.transport import reset_request_trace, set_request_trace, stage_http_request
from backend.oai_payment_extractor.web.tasks import TaskManager, chinese_failure_reason, classify_failure
from backend.oai_payment_extractor.web.proxy_probe import probe_proxy
import backend.payment_extractor_service as payment_extractor_module
from backend.payment_extractor_service import (
    PaymentExtractorProxySource,
    PaymentExtractorService,
    PaymentExtractorServiceError,
    PaymentExtractorTaskCreate,
)
from backend.payment_tools import extract_access_tokens


def _segment(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _token(*, expires_at: datetime, email: str = "user@example.test") -> str:
    return ".".join(
        (
            _segment({"alg": "none", "typ": "JWT"}),
            _segment(
                {
                    "exp": int(expires_at.timestamp()),
                    "email": email,
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "account-test",
                        "chatgpt_plan_type": "free",
                    },
                }
            ),
            "signature",
        )
    )


def test_extract_access_tokens_recurses_normalizes_and_deduplicates() -> None:
    token = _token(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    payload = json.dumps(
        {
            "user": {"email": "session@example.test"},
            "session": {"accessToken": token},
            "duplicates": [f"Bearer {token}", {"access_token": token}],
        }
    )

    result = extract_access_tokens(payload)

    assert result.count == 1
    item = result.items[0]
    assert item.token == token
    assert item.email == "session@example.test"
    assert item.accountId == "account-test"
    assert item.planType == "free"
    assert item.expired is False
    assert token not in item.preview


def test_oaics_custom_method_matches_only_requested_payment_method() -> None:
    payload = {
        "custom_payment_methods": [
            {"id": "cpmt_momo", "payment_method_type": "momo_wallet", "provider": "adyen"},
            {"id": "cpmt_gcash", "payment_method_type": "gcash", "provider": "adyen"},
        ]
    }

    assert oaics_custom_method_id(payload, "gcash") == "cpmt_gcash"
    assert oaics_custom_method_id(payload, "momo") == "cpmt_momo"
    assert oaics_custom_method_id(payload, "kakao_pay") == ""


def test_zero_paypal_confirm_matches_compact_approved_template(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200
        text = '{"submission_attempt":{"state":"requires_approval"}}'

        @staticmethod
        def json() -> dict:
            return {"submission_attempt": {"state": "requires_approval"}}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs.get("data") or {})
        return Response()

    monkeypatch.setattr(cs_live_module, "stage_http_request", fake_request)
    result = cs_live_module.stripe_confirm_cs_live(
        object(),
        {"cs_id": "cs_fixture", "publishable_key": "pk_fixture", "billing_country": "DE"},
        {"init_checksum": "checksum", "consent_collection": {}},
        {
            "checkout_amount": "0",
            "stripe_js_id": "js_fixture",
            "config_id": "cfg_fixture",
            "elements_session_id": "elements_session_fixture",
            "elements_session_config_id": "elements_config_fixture",
        },
        "https://checkout.stripe.com/c/pay/cs_fixture",
        "paypal",
        "pm_paypal_fixture",
        {"name": "Test", "email": "test@example.test", "line1": "One", "city": "Berlin", "postal_code": "10115", "country": "DE"},
        None,
    )

    assert result["submission_attempt"]["state"] == "requires_approval"
    assert captured["payment_method"] == "pm_paypal_fixture"
    assert "payment_method_data[type]" not in captured
    assert captured["expected_amount"] == "0"
    assert captured["eid"] == "NA"
    assert captured["client_attribution_metadata[merchant_integration_version]"] == "custom_checkout"
    assert "elements_session_client[session_id]" not in captured
    assert "payment_method_data[type]" not in captured


def test_paid_paypal_confirm_uses_compact_precreated_payment_method(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200
        text = '{"submission_attempt":{"state":"requires_approval"}}'

        @staticmethod
        def json() -> dict:
            return {"submission_attempt": {"state": "requires_approval"}}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs.get("data") or {})
        return Response()

    monkeypatch.setattr(cs_live_module, "stage_http_request", fake_request)
    cs_live_module.stripe_confirm_cs_live(
        object(),
        {"cs_id": "cs_fixture", "publishable_key": "pk_fixture", "billing_country": "GB"},
        {"init_checksum": "checksum", "consent_collection": {}},
        {"checkout_amount": "2000", "stripe_js_id": "js_fixture", "config_id": "cfg_fixture"},
        "https://checkout.stripe.com/c/pay/cs_fixture",
        "paypal",
        "pm_paypal_fixture",
        {"name": "Test", "email": "test@example.test", "line1": "One", "city": "London", "postal_code": "SW1A 1AA", "country": "GB"},
        None,
    )

    assert captured["payment_method"] == "pm_paypal_fixture"
    assert captured["expected_amount"] == "2000"
    assert "elements_session_client[session_id]" not in captured


def test_upi_confirm_uses_precreated_payment_method_and_compact_form(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200
        text = '{"next_action":{}}'

        @staticmethod
        def json() -> dict:
            return {"next_action": {}}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs.get("data") or {})
        return Response()

    monkeypatch.setattr(cs_live_module, "stage_http_request", fake_request)
    cs_live_module.stripe_confirm_cs_live(
        object(),
        {"cs_id": "cs_fixture", "publishable_key": "pk_fixture", "billing_country": "IN"},
        {"init_checksum": "checksum", "consent_collection": {}},
        {"checkout_amount": "0", "stripe_js_id": "js_fixture", "config_id": "cfg_fixture"},
        "https://checkout.stripe.com/c/pay/cs_fixture",
        "upi",
        "pm_upi_fixture",
        {"name": "Test", "email": "test@example.test", "line1": "One", "city": "Kolkata", "postal_code": "700016", "country": "IN", "state": "West Bengal"},
        None,
    )

    assert captured["payment_method"] == "pm_upi_fixture"
    assert captured["expected_payment_method_type"] == "upi"
    assert "payment_method_data[type]" not in captured
    assert "elements_session_client[session_id]" not in captured


def test_stripe_confirm_return_url_uses_hosted_success_return_url() -> None:
    url = stripe_confirm_return_url(
        {"cs_id": "cs_fixture", "processor_entity": "openai_ie"},
        "https://checkout.stripe.com/c/pay/cs_fixture",
    )
    assert "success_return_url=" in url
    assert "return_url=" not in url.replace("success_return_url=", "")


@pytest.mark.parametrize(
    ("raw_error", "expected"),
    [
        ("checkout_creation_rate_limited: Too many checkout attempts", "限流"),
        ("ChatGPT manual approval blocked", "结账审批被服务端拒绝"),
        ("OAICS 未返回目标支付方式 gcash 的 cpmt_ 通道", "没有所选支付方式资格"),
    ],
)
def test_payment_failure_reason_exposes_rate_limit_and_method_eligibility(
    raw_error: str, expected: str
) -> None:
    assert expected in chinese_failure_reason(raw_error)


def test_extract_access_tokens_supports_separator_and_expired_flag() -> None:
    expired = _token(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    valid = _token(
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        email="second@example.test",
    )

    result = extract_access_tokens(f"Authorization: Bearer {expired}\n---\n{valid}")

    assert result.count == 2
    assert [item.expired for item in result.items] == [True, False]


class FakeManager:
    def __init__(self) -> None:
        self.configs: list[ExtractionConfig] = []
        self.snapshots: dict[str, dict] = {}
        self.closed = False

    def create(self, config: ExtractionConfig) -> dict:
        task_id = f"task-{len(self.configs) + 1}"
        self.configs.append(config)
        snapshot = {
            "ok": True,
            "task_id": task_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "account_email": "user@example.test",
            "payment_method": config.payment_method,
            "billing_country": config.country,
            "checkout_proxy": config.checkout_proxy,
        }
        self.snapshots[task_id] = snapshot
        return dict(snapshot)

    def list(self) -> list[dict]:
        return [dict(item) for item in self.snapshots.values()]

    def get(self, task_id: str):
        item = self.snapshots.get(task_id)
        return dict(item) if item else None

    def cancel(self, task_id: str) -> dict:
        self.snapshots[task_id]["status"] = "cancelled"
        return dict(self.snapshots[task_id])

    def retry(self, task_id: str, **kwargs) -> dict:
        _ = kwargs
        return self.create(self.configs[-1])

    def resolve_paypal(self, task_id: str) -> dict:
        return dict(self.snapshots[task_id])

    def delete(self, task_id: str) -> dict:
        self.snapshots.pop(task_id)
        return {"ok": True, "task_id": task_id, "status": "deleted"}

    def delete_by_statuses(self, statuses: set[str]) -> dict:
        ids = [key for key, item in self.snapshots.items() if item["status"] in statuses]
        for task_id in ids:
            self.snapshots.pop(task_id)
        return {"ok": True, "deleted_count": len(ids), "task_ids": ids}

    def close(self, wait: bool = True) -> None:
        _ = wait
        self.closed = True


def task_payload(**overrides: object) -> PaymentExtractorTaskCreate:
    payload: dict[str, object] = {
        "accessToken": _token(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        ),
        "checkoutProxy": "http://user:CHECKOUT_SECRET@one.example:8080\nhttp://user:SECOND_SECRET@two.example:8080",
        "updateProxy": "socks5h://user:UPDATE_SECRET@update.example:1080",
        "country": "DE",
        "paymentMethod": "paypal",
        "applyCheckoutUpdate": True,
    }
    payload.update(overrides)
    return PaymentExtractorTaskCreate.model_validate(payload)


def test_service_exposes_all_source_options_and_rotates_proxy_pool(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_STICKY_TASK_PROXY", "false")
    manager = FakeManager()
    service = PaymentExtractorService(manager=manager)  # type: ignore[arg-type]

    first = service.create(task_payload())
    second = service.create(task_payload(paymentMethod="gopay", country="ID"))

    assert len(service.options()["countries"]) == 20
    assert [item["value"] for item in service.options()["paymentMethods"]] == [
        "card",
        "paypal",
        "gopay",
        "gcash",
        "ideal",
        "upi",
        "pix",
        "blik",
        "twint",
        "kakao_pay",
        "momo",
    ]
    assert manager.configs[0].checkout_proxy.endswith("@one.example:8080")
    assert manager.configs[1].checkout_proxy.endswith("@two.example:8080")
    assert manager.configs[0].update_proxy.endswith("@update.example:1080")
    assert first["checkoutProxyPreview"] == "http://***@one.example:8080"
    assert second["paymentMethod"] == "gopay"
    serialized = json.dumps([first, second])
    assert "CHECKOUT_SECRET" not in serialized
    assert "UPDATE_SECRET" not in serialized


def test_payment_method_profiles_validate_country_and_expose_currency() -> None:
    service = PaymentExtractorService(manager=FakeManager())  # type: ignore[arg-type]

    methods = {item["value"]: item for item in service.options()["paymentMethods"]}
    assert methods["pix"]["country"] == "BR"
    assert methods["pix"]["currency"] == "BRL"
    assert methods["upi"]["result_kind"] == "qr_or_deep_link"
    assert methods["blik"]["enabled"] is False
    assert payment_currency("BR", "paypal") == "USD"
    assert payment_currency("BR", "pix") == "BRL"

    with pytest.raises(PaymentExtractorServiceError, match="仅支持账单国家 PH"):
        service.create(task_payload(paymentMethod="gcash", country="DE"))
    with pytest.raises(PaymentExtractorServiceError, match="安全模式"):
        service.create(task_payload(paymentMethod="blik", country="PL"))


def test_extract_redirect_supports_wallet_qr_and_deep_link_payloads() -> None:
    assert extract_redirect_to_url(
        {"next_action": {"pix_display_qr_code": {"data": "PIX-COPY-PASTE"}}}
    ) == "PIX-COPY-PASTE"
    assert extract_redirect_to_url(
        {"payment_intent": {"next_action": {"mobile_app_redirect": {"deep_link": "upi://pay?fixture"}}}}
    ) == "upi://pay?fixture"
    assert extract_redirect_to_url(
        {"next_action": {"gopay_redirect": {"deep_link": "gopay://pay/fixture"}}}
    ) == "gopay://pay/fixture"
    assert extract_redirect_to_url(
        {"next_action": {"upi_handle_redirect_or_display_qr_code": {
            "qr_code": {"data": "upi://pay?pa=fixture"}
        }}}
    ) == "upi://pay?pa=fixture"
    assert extract_redirect_to_url(
        {"next_action": {"momo_handle_redirect_or_display_qr_code": {
            "hosted_instructions_url": "https://payment.momo.vn/fixture"
        }}}
    ) == "https://payment.momo.vn/fixture"


def test_unknown_checkout_amount_is_not_treated_as_zero() -> None:
    with pytest.raises(Exception, match="未返回应付金额"):
        checkout_payable_amount({"currency": "USD"})


@pytest.mark.parametrize(("payment_method", "expected_timeout"), [("gopay", 90), ("momo", 60)])
def test_local_wallets_use_provider_specific_poll_timeout(
    monkeypatch, payment_method: str, expected_timeout: int
) -> None:
    captured: dict[str, object] = {}

    def fake_poll(_stripe, _checkout, method, timeout, _log, _ctx):
        captured.update({"method": method, "timeout": timeout})
        return "https://provider.example/fixture"

    monkeypatch.setattr(cs_live_module, "stripe_provider_poll", fake_poll)
    result = cs_live_module.provider_redirect_after_confirm(
        object(), object(), {"cs_id": "cs_fixture"}, {}, payment_method, None, {}
    )

    assert result == "https://provider.example/fixture"
    assert captured == {"method": payment_method, "timeout": expected_timeout}


def test_checkout_approval_blocked_rotates_proxy_and_retries_same_step(monkeypatch) -> None:
    approval_proxies: list[str] = []
    results = iter(("blocked", "approved"))

    class Response:
        status_code = 200
        text = ""

        def __init__(self, result: str) -> None:
            self._result = result

        def json(self) -> dict[str, str]:
            return {"result": self._result}

    def fake_request(session, _stage, _method, url, *_args, **_kwargs):
        if url.endswith("/sentinel/ping"):
            raise RuntimeError("skip sentinel in test")
        approval_proxies.append(session.proxies["https"])
        return Response(next(results))

    monkeypatch.setattr(cs_live_module, "stage_http_request", fake_request)
    session = SimpleNamespace(proxies={"https": "http://proxy-one.example:8080"})

    cs_live_module.chatgpt_approve(
        session,
        {"cs_id": "cs_live_fixture", "billing_country": "GB", "processor_entity": "openai_llc"},
        None,
        ("http://proxy-one.example:8080", "http://proxy-two.example:8080"),
    )

    assert approval_proxies == [
        "http://proxy-one.example:8080",
        "http://proxy-two.example:8080",
    ]


def test_kakao_wallet_preconfirm_uses_dedicated_protocol_endpoint() -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = '{"accepted":true}'

        @staticmethod
        def json() -> dict[str, bool]:
            return {"accepted": True}

    class Session:
        def request(self, method: str, url: str, **kwargs):
            captured.update({"method": method, "url": url, "data": kwargs.get("data")})
            return Response()

    payload = stripe_wallet_pre_confirm(
        Session(), {"cs_id": "cs_live_fixture", "publishable_key": "pk_test_fixture"},
        "kakao_pay", None,
    )

    assert payload["accepted"] is True
    assert captured["url"] == "https://api.stripe.com/v1/payment_pages/cs_live_fixture/pre_confirm"
    assert captured["data"]["payment_method_type"] == "kakao_pay"


def test_request_trace_records_response_and_redacts_secrets() -> None:
    events: list[tuple[str, dict]] = []

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict:
            return {"state": "eligible", "client_secret": "must-not-leak", "amount": 0}

    class Session:
        @staticmethod
        def request(*_args, **_kwargs):
            return Response()

    token = set_request_trace(lambda stage, details: events.append((stage, details)))
    try:
        stage_http_request(
            Session(), "资格检测", "POST", "https://chatgpt.com/test?token=hidden",
            json={"campaign": "plus", "access_token": "must-not-leak"},
        )
    finally:
        reset_request_trace(token)

    assert events[0][0] == "http_request"
    serialized = json.dumps(events, ensure_ascii=False)
    assert "eligible" in serialized
    assert "must-not-leak" not in serialized
    assert "https://chatgpt.com/test" in serialized
    assert "token=hidden" not in serialized


def test_service_prepares_rotating_proxy_sequence_for_automatic_retries(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_STICKY_TASK_PROXY", "false")
    manager = FakeManager()
    service = PaymentExtractorService(manager=manager)  # type: ignore[arg-type]

    service.create(task_payload(autoRetryCount=2))

    config = manager.configs[0]
    assert config.auto_retry_count == 2
    assert config.checkout_proxy.endswith("@one.example:8080")
    assert config.retry_checkout_proxies[0].endswith("@two.example:8080")
    assert config.retry_checkout_proxies[1].endswith("@one.example:8080")
    assert len(config.retry_update_proxies) == 2


def test_explicit_billing_country_is_not_overridden_by_proxy_region() -> None:
    manager = FakeManager()
    service = PaymentExtractorService(manager=manager)  # type: ignore[arg-type]

    try:
        service.create(
            task_payload(
                country="DE",
                checkoutProxy=(
                    "socks5://user:SECRET@us.example-region-BR-sid-fixture:3000"
                ),
            )
        )
        assert manager.configs[0].country == "DE"
    finally:
        service.close()


def test_sticky_task_proxy_reuses_checkout_proxy_for_update(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_STICKY_TASK_PROXY", "true")
    manager = FakeManager()
    service = PaymentExtractorService(manager=manager)  # type: ignore[arg-type]

    try:
        service.create(task_payload(updateProxy="http://different.example:8080"))
        assert manager.configs[0].checkout_proxy == manager.configs[0].update_proxy
        assert service.options()["stickyTaskProxy"] is True
    finally:
        service.close()


def test_account_identity_overrides_shared_billing_identity(monkeypatch) -> None:
    token = ".".join(
        (
            _segment({"alg": "none", "typ": "JWT"}),
            _segment(
                {
                    "https://api.openai.com/profile": {
                        "email": "account@example.test",
                        "name": "Account Fixture",
                    }
                }
            ),
            "signature",
        )
    )
    captured: dict[str, object] = {}

    def fake_provider(_config, _chatgpt, _stripe, _checkout, billing, _log, **_kwargs):
        captured.update(billing)
        return {
            "provider_url": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            "payment_method_id": "pm_fixture",
        }

    class Session:
        def close(self) -> None:
            return None

    monkeypatch.setattr(application, "extract_cs_live_provider", fake_provider)
    monkeypatch.setattr(
        application,
        "create_checkout",
        lambda *_args: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "DE",
            "currency": "EUR",
        },
    )
    monkeypatch.setattr(application, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(application, "checkout_payable_amount", lambda *_args: (0, "EUR"))
    factory = SimpleNamespace(
        chatgpt=lambda *_args: Session(),
        stripe=lambda *_args: Session(),
    )
    config = ExtractionConfig(
        access_token=token,
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="DE",
        payment_method="paypal",
        apply_checkout_update=False,
    )

    result = application.extract_payment_link(config, transport_factory=factory)

    assert captured["email"] == "account@example.test"
    assert captured["name"] == "Account Fixture"
    assert result.billing.email == "account@example.test"


def test_direct_card_returns_zero_checkout_link_without_provider_confirm(monkeypatch) -> None:
    checkout = {
        "cs_id": "cs_card_fixture",
        "session_kind": "stripe_checkout",
        "processor_entity": "openai_ie",
        "billing_country": "PH",
        "currency": "PHP",
        "checkout_state": {
            "currency": "PHP",
            "total": {"total": {"minorUnitsAmount": 0}},
        },
    }
    monkeypatch.setattr(application, "check_coupon_eligibility", lambda *_args: {"state": "eligible"})
    monkeypatch.setattr(application, "create_checkout", lambda *_args: dict(checkout))
    monkeypatch.setattr(application, "update_checkout", lambda *_args: {"success": True})
    monkeypatch.setattr(application, "require_country_currency", lambda *_args: None)

    class Session:
        def close(self) -> None:
            return None

    factory = SimpleNamespace(
        chatgpt=lambda *_args: Session(),
        stripe=lambda *_args: pytest.fail("direct card must not create a Stripe provider session"),
    )
    config = ExtractionConfig(
        access_token="header.payload.signature",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://update.example:8080",
        country="PH",
        payment_method="card",
        checkout_ui_mode="auto",
        require_zero=True,
    )

    result = application.extract_payment_link(config, transport_factory=factory)

    assert result.provider_url == "https://chatgpt.com/checkout/openai_ie/cs_card_fixture"
    assert result.to_dict()["card_url"] == result.provider_url
    assert result.extra["link_type"] == "direct_card_checkout_link"
    assert result.amount_due_minor == 0


def test_service_requires_update_proxy_only_when_update_is_enabled(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_STICKY_TASK_PROXY", "false")
    service = PaymentExtractorService(manager=FakeManager())  # type: ignore[arg-type]

    with pytest.raises(PaymentExtractorServiceError) as exc_info:
        service.create(task_payload(updateProxy=""))
    assert exc_info.value.code == "update_proxy_required"

    created = service.create(
        task_payload(updateProxy="", applyCheckoutUpdate=False)
    )
    assert created["status"] == "queued"


def test_vendor_proxy_bridge_is_started_lazily_and_stopped_with_service(
    monkeypatch,
) -> None:
    starts: list[str] = []
    stops: list[str] = []
    manager = FakeManager()
    monkeypatch.setattr(
        payment_extractor_module,
        "ensure_background_server",
        lambda: starts.append("started") or True,
    )
    monkeypatch.setattr(
        payment_extractor_module,
        "stop_background_server",
        lambda: stops.append("stopped"),
    )
    service = PaymentExtractorService(manager=manager)  # type: ignore[arg-type]

    created = service.create(
        task_payload(
            checkoutProxy="proxy.iproyal.net:5959:ACCOUNT:PROXY_SECRET",
            updateProxy="",
            applyCheckoutUpdate=False,
        )
    )
    service.close()

    assert starts == ["started"]
    assert stops == ["stopped"]
    assert manager.closed is True
    assert "PROXY_SECRET" not in json.dumps(created)


def test_iprocket_bridge_falls_back_to_direct_upstream_without_preproxy(
    monkeypatch,
) -> None:
    connections: list[tuple[str, int]] = []
    http_connects: list[tuple[str, int]] = []

    class FakeSocket:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = False
            self.timeouts: list[float | None] = []

        def settimeout(self, value: float | None) -> None:
            self.timeouts.append(value)

        def close(self) -> None:
            self.closed = True

    preproxy_socket = FakeSocket("preproxy")
    direct_socket = FakeSocket("direct")

    def create_connection(address: tuple[str, int], timeout: int):
        assert timeout == 15
        connections.append(address)
        return preproxy_socket if len(connections) == 1 else direct_socket

    def socks_connect(sock, *_args, **_kwargs) -> None:
        if sock is preproxy_socket:
            raise ConnectionError("fixture preproxy unavailable")

    def http_proxy_connect(sock, host: str, port: int, *_args) -> None:
        assert sock is direct_socket
        http_connects.append((host, port))

    monkeypatch.setattr(bridge_module.socket, "create_connection", create_connection)
    monkeypatch.setattr(bridge_module, "socks_connect", socks_connect)
    monkeypatch.setattr(bridge_module, "http_proxy_connect", http_proxy_connect)

    result = bridge_module.open_chain(
        "destination.example",
        443,
        credential=("http", "proxy.1024proxy.io", 1234, "ACCOUNT", "TOKEN"),
    )

    assert result is direct_socket
    assert connections == [
        (bridge_module.LOCAL_SOCKS_HOST, bridge_module.LOCAL_SOCKS_PORT),
        ("proxy.1024proxy.io", 1234),
    ]
    assert preproxy_socket.closed is True
    assert direct_socket.timeouts == [30, None]
    assert http_connects == [("destination.example", 443)]


def test_proxy_subscription_reads_iprocket_without_real_network(monkeypatch) -> None:
    requested: list[tuple[str, int]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == 1024 * 1024
            return (
                b"http://fixture-user:FIRST_SECRET@one.example:8080\n"
                b"\n"
                b"socks5h://fixture-user:SECOND_SECRET@two.example:1080\n"
                b"http://fixture-user:FIRST_SECRET@one.example:8080\n"
            )

    def fake_urlopen(request, *, timeout: int):
        requested.append((request.full_url, timeout))
        assert request.get_header("User-agent") == "Mozilla/5.0"
        return FakeResponse()

    monkeypatch.setattr(payment_extractor_module, "urlopen", fake_urlopen)
    service = PaymentExtractorService(manager=FakeManager())  # type: ignore[arg-type]

    result = service.proxy_source(
        PaymentExtractorProxySource(
            url="https://app.iprocket.io/api/fixture?token=SUBSCRIPTION_SECRET"
        )
    )

    assert requested == [
        (
            "https://app.iprocket.io/api/fixture?token=SUBSCRIPTION_SECRET",
            15,
        )
    ]
    assert result["count"] == 3
    assert result["uniqueCount"] == 2
    assert result["proxies"][1].startswith("socks5h://")


def test_proxy_subscription_rejects_other_hosts_and_hides_fetch_secret(
    monkeypatch,
) -> None:
    service = PaymentExtractorService(manager=FakeManager())  # type: ignore[arg-type]

    with pytest.raises(PaymentExtractorServiceError) as invalid:
        service.proxy_source(
            PaymentExtractorProxySource(url="https://other.example/fixture")
        )
    assert invalid.value.code == "proxy_source_invalid"

    fetch_secret = "SUBSCRIPTION_FETCH_SECRET"

    def failed_urlopen(*_args, **_kwargs):
        raise RuntimeError(f"upstream included {fetch_secret}")

    monkeypatch.setattr(payment_extractor_module, "urlopen", failed_urlopen)
    with pytest.raises(PaymentExtractorServiceError) as failed:
        service.proxy_source(
            PaymentExtractorProxySource(
                url=f"https://app.iprocket.io/api/fixture?token={fetch_secret}"
            )
        )

    assert failed.value.code == "proxy_source_failed"
    assert failed.value.http_status == 502
    assert fetch_secret not in failed.value.message
    assert fetch_secret not in str(failed.value)


class FakeApiService:
    def __init__(self) -> None:
        self.payload: PaymentExtractorTaskCreate | None = None
        self.closed = False
        self.concurrency = 4

    def options(self) -> dict:
        return {
            "countries": [{"code": "DE", "currency": "EUR"}],
            "paymentMethods": ["paypal", "gopay", "gcash"],
            "country": "DE",
            "paymentMethod": "paypal",
            "checkoutProxy": "",
            "updateProxy": "",
            "applyCheckoutUpdate": True,
            "concurrency": self.concurrency,
            "maxConcurrency": 10,
        }

    def set_concurrency(self, payload) -> dict:
        self.concurrency = payload.concurrency
        return {"concurrency": self.concurrency, "maxConcurrency": 10}

    def create(self, payload: PaymentExtractorTaskCreate) -> dict:
        self.payload = payload
        return {"taskId": "task-api", "status": "queued", "stage": "queued"}

    def list(self) -> list[dict]:
        return [{"taskId": "task-api", "status": "queued"}]

    def get(self, task_id: str) -> dict:
        return {"taskId": task_id, "status": "succeeded"}

    def cancel(self, task_id: str) -> dict:
        return {"taskId": task_id, "status": "cancelled"}

    def retry(self, task_id: str, _payload) -> dict:
        return {"taskId": f"{task_id}-retry", "status": "queued"}

    def resolve_paypal(self, task_id: str) -> dict:
        return {"taskId": task_id, "status": "succeeded"}

    def delete(self, task_id: str) -> dict:
        return {"taskId": task_id, "status": "deleted"}

    def bulk_delete(self, _payload) -> dict:
        return {"ok": True, "deletedCount": 1, "taskIds": ["task-api"]}

    def proxy_test(self, _payload) -> dict:
        return {"ok": True, "ip": "203.0.113.10", "countryCode": "DE"}

    def proxy_source(self, _payload) -> dict:
        return {
            "ok": True,
            "proxies": ["http://fixture-user:SOURCE_SECRET@proxy.example:8080"],
            "count": 1,
            "uniqueCount": 1,
        }

    def close(self) -> None:
        self.closed = True


class FakeWebsocketApiService(FakeApiService):
    def __init__(self) -> None:
        super().__init__()
        self.unsubscribed = False
        self._live_events: queue.Queue[dict] = queue.Queue()
        self._live_events.put(
            {
                "type": "task.ping",
                "task_id": "",
                "timestamp": "2026-08-14T00:00:01Z",
                "data": {},
            }
        )

    def subscribe(self):
        return (
            [
                {
                    "type": "task.succeeded",
                    "task_id": "task-api",
                    "timestamp": "2026-08-14T00:00:00Z",
                    "data": {
                        "checkout_proxy": "http://fixture-user:PROXY_SECRET@proxy.example:8080",
                        "access_token": "ACCESS_TOKEN_SECRET",
                        "stripe_hcaptcha_token": "HCAPTCHA_SECRET",
                        "password": "PASSWORD_SECRET",
                        "result": {
                            "currency": "EUR",
                            "provider_url": (
                                "https://www.paypal.com/agreements/approve"
                                "?ba_token=AUTH_SECRET"
                            ),
                        },
                    },
                }
            ],
            self._live_events,
        )

    def unsubscribe(self, _subscriber) -> None:
        self.unsubscribed = True

    @staticmethod
    def public_event(event: dict) -> dict:
        return PaymentExtractorService.public_event(event)


def _client(tmp_path: Path, service: object) -> TestClient:
    mongo = MongoManager(uri="mongodb://127.0.0.1:1", database_name="tools_test")
    return TestClient(
        create_app(
            settings_path=tmp_path / "settings.json",
            log_dir=tmp_path / "logs",
            mongo_manager=mongo,
            payment_extractor_service=service,  # type: ignore[arg-type]
        )
    )


def test_payment_extractor_api_replaces_simplified_checkout(tmp_path: Path) -> None:
    service = FakeApiService()
    client = _client(tmp_path, service)
    payload = task_payload().model_dump(mode="json")

    defaults = client.get("/api/payment-extractor/defaults")
    created = client.post("/api/payment-extractor/tasks", json=payload)
    listed = client.get("/api/payment-extractor/tasks")

    assert defaults.status_code == 200
    assert created.status_code == 202
    assert created.json()["taskId"] == "task-api"
    assert listed.json()["tasks"][0]["taskId"] == "task-api"
    assert service.payload is not None
    assert service.payload.checkoutProxy.startswith("http://user:")
    assert client.post("/api/tools/payment-links", json={}).status_code == 404


def test_payment_extractor_task_api_exposes_all_task_actions(tmp_path: Path) -> None:
    service = FakeApiService()
    client = _client(tmp_path, service)

    fetched = client.get("/api/payment-extractor/tasks/task-api")
    cancelled = client.post("/api/payment-extractor/tasks/task-api/cancel")
    retried = client.post(
        "/api/payment-extractor/tasks/task-api/retry",
        json={
            "checkoutProxy": "http://retry.example:8080",
            "rotateCheckoutProxy": True,
        },
    )
    resolved = client.post(
        "/api/payment-extractor/tasks/task-api/resolve-paypal"
    )
    deleted = client.delete("/api/payment-extractor/tasks/task-api")
    bulk_deleted = client.post(
        "/api/payment-extractor/tasks/bulk-delete", json={"target": "failed"}
    )
    proxy_tested = client.post(
        "/api/payment-extractor/proxy-test",
        json={"checkoutProxy": "http://fixture.example:8080"},
    )
    proxy_loaded = client.post(
        "/api/payment-extractor/proxy-source",
        json={"url": "https://app.iprocket.io/api/fixture"},
    )
    concurrency = client.put(
        "/api/payment-extractor/concurrency",
        json={"concurrency": 3},
    )

    assert fetched.json() == {"taskId": "task-api", "status": "succeeded"}
    assert cancelled.json()["status"] == "cancelled"
    assert retried.status_code == 202
    assert retried.json()["taskId"] == "task-api-retry"
    assert resolved.json()["status"] == "succeeded"
    assert deleted.json()["status"] == "deleted"
    assert bulk_deleted.json()["deletedCount"] == 1
    assert proxy_tested.json()["ip"] == "203.0.113.10"
    assert proxy_loaded.json()["uniqueCount"] == 1
    assert concurrency.json() == {"concurrency": 3, "maxConcurrency": 10}
    assert service.concurrency == 3


def test_readme_environment_defaults_and_snake_case_payload(monkeypatch, tmp_path: Path) -> None:
    pool_file = tmp_path / "proxies.txt"
    pool_file.write_text(
        "http://pool-user:POOL_SECRET@pool.example:8080\n",
        encoding="utf-8",
    )
    token = _token(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    captured: dict[str, object] = {}

    def manager_factory(**kwargs):
        captured.update(kwargs)
        return FakeManager()

    monkeypatch.setattr(payment_extractor_module, "TaskManager", manager_factory)
    monkeypatch.setenv("OPLL_AT", token)
    monkeypatch.setenv("OPLL_COUNTRY", "JP")
    monkeypatch.setenv("OPLL_FORCE_COUNTRY", "DE")
    monkeypatch.setenv("OPLL_PAYMENT_METHOD", "paypal")
    monkeypatch.setenv("OPLL_UPDATE_CHECKOUT", "false")
    monkeypatch.setenv("OPLL_TASK_WORKERS", "3")
    monkeypatch.setenv("OPLL_TASK_TTL_SECONDS", "77")
    monkeypatch.setenv("OPLL_TASK_EVENT_HISTORY_SIZE", "19")
    monkeypatch.setenv("OPLL_CHECKOUT_PROXY", "http://env.example:8080")
    monkeypatch.setenv("OPLL_UPDATE_PROXY", "http://env-update.example:8080")
    monkeypatch.setenv(
        "OPLL_PROXY_SOURCE_URL", "https://app.iprocket.io/api/subscription"
    )
    monkeypatch.setenv("OPLL_PROXY_POOL_FILE", str(pool_file))

    service = PaymentExtractorService()
    try:
        options = service.options()
        assert options["country"] == "DE"
        assert options["forceCountry"] == "DE"
        assert options["paymentMethod"] == "paypal"
        assert options["applyCheckoutUpdate"] is False
        assert options["checkoutProxy"].startswith("http://pool-user:")
        assert captured == {
            "max_workers": 10,
            "concurrency": 3,
            "ttl_seconds": 77,
            "history_size": 19,
        }

        created = service.create(
            PaymentExtractorTaskCreate.model_validate(
                {
                    "access_token": "",
                    "checkout_proxy": "",
                    "update_proxy": "",
                    "apply_checkout_update": None,
                    "payment_method": None,
                    "country": None,
                }
            )
        )
        assert created["status"] == "queued"
        assert service.manager.configs[0].access_token == token
        assert service.manager.configs[0].country == "DE"
        assert service.manager.configs[0].payment_method == "paypal"
        assert service.manager.configs[0].apply_checkout_update is False
    finally:
        service.close()


def test_readme_snake_case_aliases_and_status_urls(tmp_path: Path) -> None:
    service = FakeApiService()
    client = _client(tmp_path, service)
    token = _token(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    defaults = client.get("/api/defaults")
    created = client.post(
        "/api/tasks",
        json={
            "access_token": token,
            "checkout_proxy": "http://fixture.example:8080",
            "update_proxy": "http://fixture-update.example:8080",
            "country": "DE",
            "payment_method": "paypal",
            "apply_checkout_update": True,
        },
    )
    listed = client.get("/api/tasks")
    fetched = client.get("/api/tasks/task-api")
    cancelled = client.post("/api/tasks/task-api/cancel")
    retried = client.post(
        "/api/tasks/task-api/retry",
        json={"checkout_proxy": "http://retry.example:8080"},
    )
    resolved = client.post("/api/tasks/task-api/resolve-paypal")
    deleted = client.delete("/api/tasks/task-api")
    bulk = client.post("/api/tasks/bulk-delete", json={"target": "failed"})
    proxy_tested = client.post(
        "/api/proxy/test", json={"checkout_proxy": "http://fixture.example:8080"}
    )
    proxy_loaded = client.get(
        "/api/proxy/source?url=https%3A%2F%2Fapp.iprocket.io%2Fapi%2Ffixture"
    )

    assert defaults.status_code == 200
    assert "payment_methods" in defaults.json()
    assert created.status_code == 202
    assert created.json()["task_id"] == "task-api"
    assert created.json()["status_url"] == "/api/tasks/task-api"
    assert created.json()["websocket_url"] == "/ws/tasks"
    assert listed.json()["tasks"][0]["task_id"] == "task-api"
    assert fetched.json()["task_id"] == "task-api"
    assert cancelled.json()["task_id"] == "task-api"
    assert retried.status_code == 202
    assert retried.json()["task_id"] == "task-api-retry"
    assert retried.json()["status_url"].startswith("/api/tasks/")
    assert resolved.json()["status"] == "succeeded"
    assert deleted.json()["status"] == "deleted"
    assert bulk.json()["deleted_count"] == 1
    assert proxy_tested.json()["country_code"] == "DE"
    assert proxy_loaded.json()["unique_count"] == 1
    assert service.payload is not None
    assert service.payload.accessToken == token


def test_readme_password_auth_and_websocket_event_redaction(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPLL_WEB_PASSWORD", "fixture-password")
    service = FakeWebsocketApiService()
    client = _client(tmp_path, service)

    assert client.get("/api/defaults").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/proxies").status_code != 401
    authorized = client.get(
        "/api/defaults", headers={"X-Workbench-Password": "fixture-password"}
    )
    assert authorized.status_code == 200

    with client.websocket_connect("/ws/tasks") as websocket:
        websocket.send_json({"type": "auth", "password": "fixture-password"})
        assert websocket.receive_json() == {"type": "auth.ok"}
        event = websocket.receive_json()
        serialized = json.dumps(event, ensure_ascii=False)
        assert event["type"] == "task.succeeded"
        assert "PROXY_SECRET" not in serialized
        assert "ACCESS_TOKEN_SECRET" not in serialized
        assert "HCAPTCHA_SECRET" not in serialized
        assert "PASSWORD_SECRET" not in serialized
        assert "AUTH_SECRET" not in serialized
        assert "provider_url" not in serialized
        assert "checkout_proxy" in event["data"]
        assert "***@proxy.example:8080" in event["data"]["checkout_proxy"]

    assert service.unsubscribed is True


def test_payment_extractor_lifespan_closes_injected_service(tmp_path: Path) -> None:
    service = FakeApiService()

    with _client(tmp_path, service) as client:
        assert client.get("/api/payment-extractor/defaults").status_code == 200
        assert service.closed is False

    assert service.closed is True


def test_payment_extractor_api_returns_structured_validation_failures(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPLL_STICKY_TASK_PROXY", "false")
    service = PaymentExtractorService(
        manager=FakeManager(),  # type: ignore[arg-type]
        task_limit=1,
    )
    client = _client(tmp_path, service)

    missing_update = task_payload(updateProxy="").model_dump(mode="json")
    response = client.post("/api/payment-extractor/tasks", json=missing_update)
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "update_proxy_required",
        "message": "启用 Checkout Update 时必须填写 Update 代理",
    }

    first = client.post(
        "/api/payment-extractor/tasks",
        json=task_payload(applyCheckoutUpdate=False).model_dump(mode="json"),
    )
    queue_full = client.post(
        "/api/payment-extractor/tasks",
        json=task_payload(applyCheckoutUpdate=False).model_dump(mode="json"),
    )
    assert first.status_code == 202
    assert queue_full.status_code == 409
    assert queue_full.json()["detail"]["code"] == "extractor_queue_full"

    invalid_source = client.post(
        "/api/payment-extractor/proxy-source",
        json={"url": "https://other.example/fixture"},
    )
    assert invalid_source.status_code == 422
    assert invalid_source.json()["detail"]["code"] == "proxy_source_invalid"

    invalid_payload = client.post(
        "/api/payment-extractor/tasks",
        json={
            **task_payload().model_dump(mode="json"),
            "country": "ZZ",
            "unexpected": "rejected",
        },
    )
    assert invalid_payload.status_code == 422


def test_payment_extractor_default_queue_capacity_is_ten_thousand(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPLL_TASK_LIMIT", raising=False)
    service = PaymentExtractorService(manager=FakeManager())  # type: ignore[arg-type]
    try:
        assert service.options()["taskLimit"] == 10_000
    finally:
        service.close()


@pytest.mark.parametrize(
    ("session_kind", "extractor_name", "provider_field"),
    [
        ("stripe_checkout", "extract_cs_live_provider", "paypal_url"),
        ("openai_custom_checkout", "extract_oaics_provider", "paypal_url"),
    ],
)
def test_vendored_application_routes_both_checkout_branches(
    monkeypatch, session_kind: str, extractor_name: str, provider_field: str
) -> None:
    checkout = {
        "cs_id": "cs_test_fixture" if session_kind == "stripe_checkout" else "oaics_fixture",
        "session_kind": session_kind,
        "billing_country": "DE",
        "currency": "EUR",
    }
    calls: list[str] = []

    monkeypatch.setattr(application, "check_coupon_eligibility", lambda *_args: None)
    monkeypatch.setattr(application, "create_checkout", lambda *_args: dict(checkout))
    monkeypatch.setattr(application, "update_checkout", lambda *_args: None)
    monkeypatch.setattr(application, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(application, "checkout_payable_amount", lambda *_args: (0, "EUR"))

    def fake_provider(*_args, **_kwargs):
        calls.append(extractor_name)
        return {
            "provider_url": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            provider_field: "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            "payment_method_id": "pm_fixture",
        }

    monkeypatch.setattr(application, extractor_name, fake_provider)
    other = (
        "extract_oaics_provider"
        if extractor_name == "extract_cs_live_provider"
        else "extract_cs_live_provider"
    )
    monkeypatch.setattr(
        application,
        other,
        lambda *_args, **_kwargs: pytest.fail("wrong checkout branch"),
    )

    class Session:
        def close(self) -> None:
            return None

    factory = SimpleNamespace(
        chatgpt=lambda *_args: Session(),
        stripe=lambda *_args: Session(),
    )
    config = ExtractionConfig(
        access_token="header.payload.signature",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://update.example:8080",
        country="DE",
        payment_method="paypal",
    )

    result = application.extract_payment_link(config, transport_factory=factory)

    assert calls == [extractor_name]
    assert result.provider_url.endswith("BA-FIXTURE")
    assert result.session_kind == session_kind


def test_task_manager_result_is_sanitized_by_adapter() -> None:
    def fake_extractor(config: ExtractionConfig, **_kwargs) -> PaymentLinkResult:
        return PaymentLinkResult(
            checkout_session_id="cs_test_fixture",
            session_kind="stripe_checkout",
            payment_method=config.payment_method,
            billing_country=config.country,
            currency="EUR",
            amount_due=0,
            amount_due_minor=0,
            billing=application.billing_for_country(config.country),
            account_email="user@example.test",
            provider_url="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
            provider_field="paypal_url",
            provider_value="https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE",
        )

    manager = TaskManager(extractor=fake_extractor, max_workers=1)
    service = PaymentExtractorService(manager=manager)
    try:
        created = service.create(task_payload())
        deadline = time.monotonic() + 3
        snapshot = created
        while snapshot["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            snapshot = service.get(created["taskId"])
        serialized = json.dumps(snapshot)
        assert snapshot["status"] == "succeeded"
        assert snapshot["result"]["paypalUrl"].endswith("BA-FIXTURE")
        assert "CHECKOUT_SECRET" not in serialized
        assert "UPDATE_SECRET" not in serialized
        assert task_payload().accessToken not in serialized
    finally:
        service.close()


def test_task_manager_runtime_concurrency_releases_queued_tasks() -> None:
    started: queue.Queue[str] = queue.Queue()
    release = threading.Event()

    def blocking_extractor(config: ExtractionConfig, **_kwargs) -> dict:
        started.put(config.access_token)
        assert release.wait(3)
        return {"ok": True}

    manager = TaskManager(
        extractor=blocking_extractor,  # type: ignore[arg-type]
        max_workers=3,
        concurrency=1,
    )
    config = ExtractionConfig(
        access_token="header.payload.signature",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://update.example:8080",
        country="DE",
        payment_method="paypal",
    )
    try:
        task_ids = [manager.create(config)["task_id"] for _ in range(3)]
        assert started.get(timeout=1) == config.access_token
        with pytest.raises(queue.Empty):
            started.get(timeout=0.2)

        assert manager.set_concurrency(2) == 2
        assert manager.concurrency == 2
        assert started.get(timeout=1) == config.access_token

        release.set()
        deadline = time.monotonic() + 3
        while any(
            manager.get(task_id)["status"] not in {"succeeded", "failed", "cancelled"}
            for task_id in task_ids
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        release.set()
        manager.close()


def test_task_manager_failure_redacts_all_configured_secrets() -> None:
    def failing_extractor(config: ExtractionConfig, **kwargs) -> PaymentLinkResult:
        kwargs["stage_callback"]("checkout")
        raise RuntimeError(
            "fixture failure HTTP status: 403 "
            f"token={config.access_token} "
            f"checkout={config.checkout_proxy} "
            f"update={config.update_proxy} "
            f"hcaptcha={config.stripe_hcaptcha_token}"
        )

    manager = TaskManager(extractor=failing_extractor, max_workers=1)
    service = PaymentExtractorService(manager=manager)
    payload = task_payload(stripeHcaptchaToken="HCAPTCHA_SECRET")
    try:
        created = service.create(payload)
        deadline = time.monotonic() + 3
        snapshot = created
        while snapshot["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            snapshot = service.get(created["taskId"])

        serialized = json.dumps(snapshot)
        assert snapshot["status"] == "failed"
        assert snapshot["networkError"] is False
        assert snapshot["failureStage"] == "checkout"
        assert snapshot["errorKind"] == "RuntimeError"
        assert snapshot["errorHttpStatus"] == 403
        assert "***" in snapshot["error"]
        for secret in (
            payload.accessToken,
            "CHECKOUT_SECRET",
            "UPDATE_SECRET",
            "HCAPTCHA_SECRET",
        ):
            assert secret not in serialized
    finally:
        service.close()


def test_task_manager_automatically_retries_and_translates_failure() -> None:
    attempts: list[str] = []

    def flaky_extractor(config: ExtractionConfig, **_kwargs) -> dict:
        attempts.append(config.checkout_proxy)
        if len(attempts) < 3:
            raise RuntimeError("Proxy CONNECT aborted")
        return {"ok": True, "provider_url": "https://pay.example.test/success"}

    manager = TaskManager(extractor=flaky_extractor, max_workers=1)  # type: ignore[arg-type]
    config = ExtractionConfig(
        access_token="header.payload.signature",
        checkout_proxy="http://proxy-one.example:8080",
        update_proxy="http://update-one.example:8080",
        country="DE",
        payment_method="paypal",
        auto_retry_count=2,
        retry_checkout_proxies=(
            "http://proxy-two.example:8080",
            "http://proxy-three.example:8080",
        ),
        retry_update_proxies=(
            "http://update-two.example:8080",
            "http://update-three.example:8080",
        ),
    )
    try:
        task_id = manager.create(config)["task_id"]
        deadline = time.monotonic() + 3
        snapshot = manager.get(task_id)
        while snapshot and snapshot["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            snapshot = manager.get(task_id)
        assert snapshot is not None
        assert snapshot["status"] == "succeeded"
        assert snapshot["attempt"] == 3
        assert snapshot["max_attempts"] == 3
        assert attempts == [
            "http://proxy-one.example:8080",
            "http://proxy-two.example:8080",
            "http://proxy-three.example:8080",
        ]
    finally:
        manager.close()


@pytest.mark.parametrize(
    ("exc", "stage", "category", "retryable"),
    [
        (NetworkError("checkout", "connection reset"), "checkout", "network_error", True),
        (ProtocolError(409, "promo eligibility rejected: state=not_eligible"), "eligibility_check", "eligibility_rejected", True),
        (ProtocolError(409, "checkout trial eligibility rejected: one_click_trial_eligible=false"), "checkout_update", "eligibility_rejected", True),
        (ProtocolError(409, "final amount is not zero"), "payment_confirmation", "amount_or_currency_mismatch", True),
        (ProtocolError(409, "最终账单不是 0 元"), "redirect_resolution", "amount_or_currency_mismatch", True),
        (ProtocolError(409, "ChatGPT manual approval blocked"), "payment_confirmation", "final_approval_rejected", True),
        (ProtocolError(409, "ChatGPT 结账审批被服务端拒绝，金额或风险校验未通过"), "payment_confirmation", "final_approval_rejected", True),
    ],
)
def test_failure_classifier_drives_retry_policy(exc, stage, category, retryable) -> None:
    result = classify_failure(exc, stage)
    assert result["category"] == category
    assert result["retryable"] is retryable
    assert result["stage"] == stage


def test_checkout_update_continues_when_one_click_trial_is_false(monkeypatch) -> None:
    payload = {
        "success": True,
        "checkout_session": {"one_click_trial_eligible": False},
    }

    class Response:
        status_code = 200
        text = json.dumps(payload)

        @staticmethod
        def json() -> dict:
            return payload

    monkeypatch.setattr(
        "backend.oai_payment_extractor.checkout.stage_http_request",
        lambda *_args, **_kwargs: Response(),
    )
    session = SimpleNamespace(proxies={})
    config = ExtractionConfig(
        access_token="header.payload.signature",
        checkout_proxy="http://checkout.example:8080",
        update_proxy="http://update.example:8080",
        country="GB",
    )
    checkout = {
        "cs_id": "cs_live_fixture",
        "billing_country": "GB",
        "processor_entity": "openai_llc",
    }

    result = update_checkout(config, session, checkout, None)

    assert result["checkout_session"]["one_click_trial_eligible"] is False
    assert session.proxies["https"] == "http://checkout.example:8080"


def test_task_manager_retries_non_network_failure_with_rotated_proxy() -> None:
    attempts = 0

    def rejected_extractor(_config: ExtractionConfig, **kwargs) -> dict:
        nonlocal attempts
        attempts += 1
        kwargs["stage_callback"]("payment_confirmation")
        raise ProtocolError(409, "ChatGPT manual approval blocked")

    manager = TaskManager(extractor=rejected_extractor, max_workers=1)  # type: ignore[arg-type]
    config = ExtractionConfig(
        access_token="header.payload.signature",
        checkout_proxy="http://proxy-one.example:8080",
        update_proxy="http://update-one.example:8080",
        country="GB",
        payment_method="paypal",
        auto_retry_count=2,
        retry_checkout_proxies=("http://proxy-two.example:8080", "http://proxy-three.example:8080"),
        retry_update_proxies=("http://update-two.example:8080", "http://update-three.example:8080"),
    )
    try:
        task_id = manager.create(config)["task_id"]
        deadline = time.monotonic() + 3
        snapshot = manager.get(task_id)
        while snapshot["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            snapshot = manager.get(task_id)
        assert attempts == 3
        assert snapshot["status"] == "failed"
        assert snapshot["failure_category"] == "final_approval_rejected"
        assert snapshot["retryable"] is True
        assert snapshot["attempt"] == 3
    finally:
        manager.close()


def test_proxy_probe_records_http_tls_and_egress_metadata() -> None:
    class Socket:
        @staticmethod
        def version() -> str:
            return "TLSv1.3"

    response = SimpleNamespace(
        status_code=200,
        raw=SimpleNamespace(connection=SimpleNamespace(sock=Socket())),
        json=lambda: {
            "success": True,
            "ip": "203.0.113.10",
            "country": "United Kingdom",
            "country_code": "GB",
            "region": "England",
            "region_code": "ENG",
        },
    )

    def fake_get(*_args, **kwargs):
        assert kwargs["proxies"]["https"] == "http://proxy.example:8080"
        return response

    result = probe_proxy("http://proxy.example:8080", request_get=fake_get).to_dict()
    assert result["ip"] == "203.0.113.10"
    assert result["country_code"] == "GB"
    assert result["http_status"] == 200
    assert result["tls_version"] == "TLSv1.3"
    assert isinstance(result["latency_ms"], int)
