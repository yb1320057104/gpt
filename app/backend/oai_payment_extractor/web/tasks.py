from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import queue
import re
import threading
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..auth import account_email, normalize_access_token
from ..application import extract_payment_link
from ..errors import ConfigurationError, ExtractionCancelled, NetworkError, ProtocolError
from ..logging_utils import log_context
from ..models import ExtractionConfig, PaymentLinkResult
from .events import EVENT_HISTORY_SIZE, make_event, redact_text, utc_timestamp


TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
STAGE_PROGRESS = {
    "queued": 0,
    "running": 5,
    "eligibility_check": 10,
    "checkout": 15,
    "checkout_update": 25,
    "stripe_init": 35,
    "elements_session": 50,
    "taxes": 65,
    "payment_confirmation": 80,
    "redirect_resolution": 95,
    "completed": 100,
}


class TaskNotFoundError(KeyError):
    pass


class TaskStateError(RuntimeError):
    pass


def _has_nonzero_amount(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    value = result.get("amount_due_minor", result.get("amount_due"))
    if value is None or value == "":
        return False
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount != 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def chinese_failure_reason(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.casefold()
    mappings = (
        ("checkout_creation_rate_limited", "Checkout 创建次数过多，账号已被限流，请稍后再试或更换账号"),
        ("too many checkout attempts", "Checkout 创建次数过多，账号已被限流，请稍后再试或更换账号"),
        ("rate limit", "请求过于频繁，账号或出口 IP 已被限流，请稍后再试"),
        ("manual approval blocked", "ChatGPT 结账审批被服务端拒绝；服务端未提供具体原因，可能是账号资格、支付方式、账单信息、金额状态或风险控制校验未通过"),
        ("结账审批被服务端拒绝", "ChatGPT 结账审批被服务端拒绝；服务端未提供具体原因，可能是账号资格、支付方式、账单信息、金额状态或风险控制校验未通过"),
        ("未返回目标支付方式", "该账号在当前国家或代理出口下没有所选支付方式资格"),
        ("未提供目标支付方式", "该账号在当前国家或代理出口下没有所选支付方式资格"),
        ("target payment method", "该账号在当前国家或代理出口下没有所选支付方式资格"),
        ("billing country must match request country", "账单国家与账号或请求国家不一致，请选择与账号注册地区一致的国家"),
        ("this promotion is not available", "当前账号或地区不支持该优惠活动"),
        ("promo eligibility rejected", "当前账号不具备优惠资格"),
        ("state=not_eligible", "当前账号不具备优惠资格"),
        ("does not offer", "当前 Checkout 没有提供所选支付方式"),
        ("proxy connect aborted", "代理连接被中止，代理可能失效或不支持目标站点"),
        ("socks handshake", "SOCKS 代理握手失败，代理不可用或协议配置错误"),
        ("operation timed out", "请求超时，代理速度过慢或目标站点无响应"),
        ("connection timed out", "连接超时，代理速度过慢或无法连接目标站点"),
        ("timed out", "请求超时，代理速度过慢或目标站点无响应"),
        ("redirect poll timeout", "等待支付跳转链接超时"),
        ("connection closed", "连接被提前关闭，代理链路不稳定"),
        ("connection reset", "连接被重置，代理链路不稳定"),
        ("http 401", "身份凭据已失效或无权限（HTTP 401）"),
        ("http 403", "请求被目标站点拒绝（HTTP 403）"),
        ("http 429", "请求过于频繁，已被限流（HTTP 429）"),
    )
    for marker, message in mappings:
        if marker in lowered:
            methods = re.search(r"methods=([^\n;]+)", text, flags=re.IGNORECASE)
            return f"{message}；可用方式：{methods.group(1)}" if methods else message
    return f"提链失败：{text}" if text else "提链失败，未返回详细原因"


def _looks_like_network_error(value: Any) -> bool:
    lowered = str(value or "").casefold()
    return any(marker in lowered for marker in (
        "timeout", "timed out", "proxy connect", "socks handshake",
        "connection closed", "connection reset", "could not connect",
    ))


def classify_failure(exc: Exception, stage: str | None = None) -> dict[str, Any]:
    """Return a stable failure class shared by API output and retry policy."""
    text = str(exc or "").casefold()
    network = isinstance(exc, NetworkError) or _looks_like_network_error(text)
    if network:
        category = "network_error"
    elif "promo eligibility" in text or "trial eligibility rejected" in text or "not_eligible" in text or "promotion is not available" in text:
        category = "eligibility_rejected"
    elif "approval blocked" in text or ("审批" in text and "拒绝" in text):
        category = "final_approval_rejected"
    elif any(marker in text for marker in ("amount", "金额", "应付", "currency", "币种", "账单不是", "not zero", "non-zero")):
        category = "amount_or_currency_mismatch"
    elif isinstance(exc, ConfigurationError):
        category = "configuration_error"
    elif isinstance(exc, ProtocolError):
        category = "protocol_error"
    else:
        category = "unexpected_error"
    return {
        "category": category,
        # All extraction failures consume the configured proxy-rotation retries.
        # Category and network_error remain diagnostic fields only.
        "retryable": True,
        "network_error": network,
        "stage": str(stage or getattr(exc, "stage", "") or "unknown"),
        "error_kind": type(exc).__name__,
        "http_status": getattr(exc, "status_code", None),
    }


@dataclass
class TaskRecord:
    task_id: str
    config: ExtractionConfig
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    created_at: str = field(default_factory=utc_timestamp)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    network_error: bool = False
    failure_stage: str | None = None
    error_kind: str | None = None
    error_http_status: int | None = None
    failure_category: str | None = None
    retryable: bool = False
    account_email: str = ""
    session_kind: str | None = None
    retry_of: str | None = None
    attempt: int = 1
    logs: list[dict[str, Any]] = field(default_factory=list)


class TaskManager:
    """Thread-backed in-memory task manager with one global event stream."""

    def __init__(
        self,
        extractor: Callable[..., PaymentLinkResult] = extract_payment_link,
        *,
        max_workers: int = 2,
        concurrency: int | None = None,
        ttl_seconds: int = 3600,
        history_size: int = EVENT_HISTORY_SIZE,
    ) -> None:
        self._extractor = extractor
        self._capacity = max(1, max_workers)
        self._concurrency = max(
            1, min(self._capacity, concurrency if concurrency is not None else self._capacity)
        )
        self._active_slots = 0
        self._executor = ThreadPoolExecutor(max_workers=self._capacity, thread_name_prefix="payment-task")
        self._ttl = max(1, ttl_seconds)
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, history_size))
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    @property
    def concurrency(self) -> int:
        with self._lock:
            return self._concurrency

    @property
    def max_concurrency(self) -> int:
        return self._capacity

    def set_concurrency(self, value: int) -> int:
        normalized = max(1, min(self._capacity, int(value)))
        with self._lock:
            self._concurrency = normalized
        return normalized

    def close(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def create(self, config: ExtractionConfig) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            return self._create_locked(config)

    def retry(
        self,
        task_id: str,
        *,
        checkout_proxy: str | None = None,
        update_proxy: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status == "succeeded":
                if not _has_nonzero_amount(record.result):
                    raise TaskStateError("only succeeded tasks with a non-zero amount can be retried")
            elif record.status not in {"failed", "cancelled"}:
                raise TaskStateError("only failed, cancelled, or non-zero succeeded tasks can be retried")
            retry_config = record.config
            if checkout_proxy is not None:
                proxy = str(checkout_proxy).strip()
                if not proxy:
                    raise TaskStateError("checkout proxy is required for retry")
                retry_config = replace(record.config, checkout_proxy=proxy)
            if update_proxy is not None:
                proxy = str(update_proxy).strip()
                if not proxy:
                    raise TaskStateError("update proxy is required for retry")
                retry_config = replace(retry_config, update_proxy=proxy)
            self._tasks.pop(task_id, None)
            self._publish_locked(task_id, "task.deleted", {"status": record.status, "reason": "retry"})
            return self._create_locked(retry_config, retry_of=task_id)

    def _create_locked(self, config: ExtractionConfig, *, retry_of: str | None = None) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        record = TaskRecord(
            task_id=task_id,
            config=config,
            account_email=account_email(normalize_access_token(config.access_token)),
            retry_of=retry_of,
        )
        self._tasks[task_id] = record
        created_data: dict[str, Any] = {
            "status": "queued",
            "account_email": record.account_email,
            "payment_method": record.config.payment_method,
            "billing_country": record.config.country,
            "progress": record.progress,
        }
        if retry_of:
            created_data["retry_of"] = retry_of
        self._publish_locked(task_id, "task.created", created_data)
        log_context(component="task", task_id=task_id).info("task queued")
        record.future = self._executor.submit(self._run, task_id)
        return self._snapshot_locked(record)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            return self._snapshot_locked(record, include_logs=True) if record else None

    def list(self) -> list[dict[str, Any]]:
        """Return all non-expired task snapshots without exposing task config."""
        with self._lock:
            self._cleanup_locked()
            records = sorted(
                self._tasks.values(),
                key=lambda record: record.created_at,
                reverse=True,
            )
            return [self._snapshot_locked(record) for record in records]

    def resolve_paypal(self, task_id: str) -> dict[str, Any]:
        """Resolve an already-successful Stripe redirect into a strict BA URL."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status != "succeeded" or not isinstance(record.result, dict):
                raise TaskStateError("only succeeded PayPal tasks can be resolved")
            if record.config.payment_method != "paypal":
                raise TaskStateError("task is not PayPal")
            source_url = str(
                record.result.get("stripe_redirect_url")
                or record.result.get("provider_url")
                or record.result.get("paypal_url")
                or ""
            ).strip()
            config = record.config

        from ..stripe_common import is_paypal_ba_approval_url, resolve_external_redirect
        from ..transport import DefaultTransportFactory, safe_close

        stripe = DefaultTransportFactory().stripe(config)
        try:
            final_url = resolve_external_redirect(
                stripe,
                source_url,
                preferred_hosts=("paypal.com",),
                max_hops=8,
            )
        finally:
            safe_close(stripe)
        if not is_paypal_ba_approval_url(final_url):
            raise TaskStateError("PayPal BA 链仍未解析成功，请更换代理后重试")

        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or not isinstance(record.result, dict):
                raise TaskNotFoundError(task_id)
            record.result["provider_url"] = final_url
            record.result["paypal_url"] = final_url
            self._publish_locked(
                task_id,
                "task.succeeded",
                {"status": record.status, "result": record.result, "progress": record.progress},
            )
            return self._snapshot_locked(record)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status in TERMINAL_STATES:
                raise TaskStateError(f"task is already {record.status}")
            record.cancel_event.set()
            log_context(component="task", task_id=task_id).info("task cancellation requested")
            if record.status == "queued":
                if record.future is not None:
                    record.future.cancel()
                record.status = "cancelled"
                record.stage = "cancelled"
                record.finished_at = utc_timestamp()
                self._publish_locked(
                    task_id,
                    "task.cancelled",
                    {"status": record.status, "progress": record.progress},
                )
            elif record.status == "running":
                record.status = "cancel_requested"
                self._publish_locked(task_id, "task.cancel_requested", {"status": record.status})
            return self._snapshot_locked(record)

    def cancel_all(self) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            task_ids: list[str] = []
            for record in self._tasks.values():
                if record.status in TERMINAL_STATES:
                    continue
                record.cancel_event.set()
                task_ids.append(record.task_id)
                if record.status == "queued":
                    if record.future is not None:
                        record.future.cancel()
                    record.status = "cancelled"
                    record.stage = "cancelled"
                    record.finished_at = utc_timestamp()
                    self._publish_locked(record.task_id, "task.cancelled", {
                        "status": record.status, "progress": record.progress, "reason": "bulk",
                    })
                elif record.status in {"running", "cancel_requested"}:
                    record.status = "cancel_requested"
                    self._publish_locked(record.task_id, "task.cancel_requested", {
                        "status": record.status, "reason": "bulk",
                    })
            return {"ok": True, "cancelled_count": len(task_ids), "task_ids": task_ids}

    def delete(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status not in {"succeeded", "failed", "cancelled"}:
                raise TaskStateError("only succeeded, failed, or cancelled tasks can be deleted")
            status = record.status
            self._tasks.pop(task_id, None)
            self._publish_locked(task_id, "task.deleted", {"status": status})
            log_context(component="task", task_id=task_id).info("task deleted")
            return {"ok": True, "task_id": task_id, "status": "deleted"}

    def delete_by_statuses(self, statuses: set[str]) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            records = [record for record in self._tasks.values() if record.status in statuses]
            task_ids = []
            for record in records:
                self._tasks.pop(record.task_id, None)
                task_ids.append(record.task_id)
                self._publish_locked(
                    record.task_id,
                    "task.deleted",
                    {"status": record.status, "reason": "bulk"},
                )
                log_context(component="task", task_id=record.task_id).info("task deleted in bulk")
            return {"ok": True, "deleted_count": len(task_ids), "task_ids": task_ids}

    def subscribe(self) -> tuple[list[dict[str, Any]], queue.Queue[dict[str, Any]]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with self._lock:
            self._cleanup_locked()
            active_task_ids = set(self._tasks)
            terminal_task_ids = {
                task_id
                for task_id, record in self._tasks.items()
                if record.status in TERMINAL_STATES
            }
            history = [
                event
                for event in self._history
                if (
                    not event.get("task_id")
                    or event["task_id"] in active_task_ids
                    and (
                        event["task_id"] not in terminal_task_ids
                        or event["type"] in {"task.succeeded", "task.failed", "task.cancelled"}
                    )
                )
            ]
            self._subscribers.add(subscriber)
        return history, subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def _acquire_slot(self, task_id: str) -> bool:
        while True:
            with self._lock:
                record = self._tasks.get(task_id)
                if record is None or record.status == "cancelled":
                    return False
                if self._active_slots < self._concurrency:
                    self._active_slots += 1
                    return True
            if record.cancel_event.wait(0.1):
                return False

    def _release_slot(self) -> None:
        with self._lock:
            self._active_slots = max(0, self._active_slots - 1)

    def _run(self, task_id: str) -> None:
        if not self._acquire_slot(task_id):
            return
        try:
            self._run_with_slot(task_id)
        finally:
            self._release_slot()

    def _run_with_slot(self, task_id: str) -> None:
        task_log = log_context(component="task", task_id=task_id)
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status == "cancelled":
                return
            record.status = "running"
            record.stage = "running"
            record.progress = STAGE_PROGRESS[record.stage]
            record.started_at = utc_timestamp()
            self._publish_locked(
                task_id,
                "task.started",
                {"status": record.status, "progress": record.progress},
            )
            self._publish_locked(task_id, "task.log", {"message": "task started"})
            self._append_log_locked(record, "started", "任务开始", "success", {
                "attempt": record.attempt,
                "billing_country": record.config.country,
                "payment_method": record.config.payment_method,
            })
            task_log.info("task started")

        if self._extractor is extract_payment_link:
            from .proxy_probe import probe_proxy
            proxy_targets = [("checkout_proxy_probe", "Checkout 代理出口检测", record.config.checkout_proxy)]
            if record.config.apply_checkout_update:
                proxy_targets.append(("update_proxy_probe", "Update 代理出口检测", record.config.update_proxy))
            for probe_step, probe_label, proxy_url in proxy_targets:
                try:
                    probe = probe_proxy(proxy_url, timeout=5.0).to_dict()
                    with self._lock:
                        current = self._tasks.get(task_id)
                        if current is not None:
                            self._append_log_locked(current, probe_step, probe_label, "success", {
                                "代理地址": self._proxy_preview(proxy_url),
                                "实际出口IP": probe.get("ip", ""),
                                "出口国家代码": probe.get("country_code") or probe.get("country") or "",
                                "出口地区": probe.get("region", ""),
                                "出口城市": probe.get("city", ""),
                                "??????": probe.get("latency_ms"),
                                "HTTP???": probe.get("http_status"),
                                "TLS??": probe.get("tls_version"),
                            })
                except Exception as exc:
                    with self._lock:
                        current = self._tasks.get(task_id)
                        if current is not None:
                            self._append_log_locked(current, probe_step, probe_label, "warning", {
                                "代理地址": self._proxy_preview(proxy_url),
                                "检测失败原因": redact_text(exc, self._secrets(current.config)),
                            })

        try:
            result = self._extractor(
                record.config,
                cancel_event=record.cancel_event,
                stage_callback=lambda stage, details=None: self._stage(task_id, stage, details or {}),
            )
            with self._lock:
                record = self._tasks.get(task_id)
                if record is None:
                    return
                if record.cancel_event.is_set() or record.status == "cancel_requested":
                    self._finish_cancelled_locked(record)
                else:
                    record.status = "succeeded"
                    record.stage = "completed"
                    record.progress = STAGE_PROGRESS[record.stage]
                    record.result = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                    record.finished_at = utc_timestamp()
                    self._append_log_locked(record, "completed", "提链完成", "success", {
                        "amount_due": record.result.get("amount_due"),
                        "currency": record.result.get("currency"),
                        "is_zero_amount": record.result.get("amount_due_minor") == 0,
                    })
                    self._publish_locked(
                        task_id,
                        "task.succeeded",
                        {
                            "status": record.status,
                            "result": record.result,
                            "checkout_proxy": record.config.checkout_proxy,
                            "progress": record.progress,
                        },
                    )
                    task_log.info("task succeeded")
        except ExtractionCancelled as exc:
            with self._lock:
                record = self._tasks.get(task_id)
                if record is not None:
                    self._finish_cancelled_locked(record, str(exc))
        except Exception as exc:
            should_retry = False
            with self._lock:
                record = self._tasks.get(task_id)
                if record is None:
                    return
                record.failure_stage = record.stage
                record.error_kind = type(exc).__name__
                status_match = re.search(
                    r"(?:HTTP(?: status)?|status(?: code)?)[\s:=]+(\d{3})",
                    str(exc),
                    flags=re.IGNORECASE,
                )
                explicit_status = getattr(exc, "status_code", None)
                record.error_http_status = (
                    int(explicit_status)
                    if isinstance(explicit_status, int)
                    else (int(status_match.group(1)) if status_match else None)
                )
                raw_error = redact_text(exc, self._secrets(record.config))
                classification = classify_failure(exc, record.failure_stage)
                record.network_error = bool(classification["network_error"])
                record.failure_category = str(classification["category"])
                record.retryable = bool(classification["retryable"])
                if record.retryable and record.attempt <= record.config.auto_retry_count and not record.cancel_event.is_set():
                    retry_index = record.attempt - 1
                    checkout_proxy = record.config.retry_checkout_proxies[retry_index]
                    update_proxy = record.config.retry_update_proxies[retry_index]
                    record.config = replace(
                        record.config,
                        checkout_proxy=checkout_proxy,
                        update_proxy=update_proxy,
                    )
                    reason = chinese_failure_reason(raw_error)
                    self._append_log_locked(record, "attempt_failed", "本次提链尝试失败", "error", {
                        "失败阶段": record.failure_stage or "",
                        "错误类型": record.error_kind or "",
                        "HTTP状态码": record.error_http_status,
                        "中文原因": reason,
                        "原始错误": raw_error,
                    })
                    record.attempt += 1
                    record.status = "queued"
                    record.stage = "queued"
                    record.progress = 0
                    record.error = None
                    record.finished_at = None
                    self._publish_locked(task_id, "task.log", {
                        "message": f"第 {record.attempt - 1} 次提链失败：{reason}；已自动更换代理，开始第 {record.attempt} 次尝试",
                    })
                    task_log.warning(
                        "attempt {} failed: {}; automatically retrying attempt {}",
                        record.attempt - 1,
                        reason,
                        record.attempt,
                    )
                    should_retry = True
                else:
                    record.status = "failed"
                    record.stage = "failed"
                    record.error = chinese_failure_reason(raw_error)
                    record.finished_at = utc_timestamp()
                    self._append_log_locked(record, "failed", "提链失败", "error", {
                        "failure_stage": record.failure_stage or "",
                        "error_kind": record.error_kind or "",
                        "http_status": record.error_http_status,
                        "message": record.error,
                        "failure_category": record.failure_category,
                        "retryable": record.retryable,
                    })
                if should_retry:
                    pass
                else:
                    self._publish_locked(
                        task_id,
                        "task.failed",
                        {
                            "status": record.status,
                            "error": record.error,
                            "network_error": record.network_error,
                            "failure_stage": record.failure_stage,
                            "error_kind": record.error_kind,
                            "error_http_status": record.error_http_status,
                            "failure_category": record.failure_category,
                            "retryable": record.retryable,
                            "progress": record.progress,
                            "attempt": record.attempt,
                            "max_attempts": record.config.auto_retry_count + 1,
                        },
                    )
                    task_log.error("task failed: {}", record.error)
            if should_retry:
                self._run_with_slot(task_id)

    def _stage(self, task_id: str, stage: str, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status in TERMINAL_STATES:
                return
            if str(stage).startswith("checkout_kind:"):
                record.session_kind = str(stage).split(":", 1)[1]
                self._publish_locked(
                    task_id,
                    "task.checkout_detected",
                    {
                        "session_kind": record.session_kind,
                        "status": record.status,
                        "progress": record.progress,
                    },
                )
                self._append_log_locked(record, "checkout_kind", "识别结账类型", "success", {
                    "session_kind": record.session_kind,
                })
                return
            if stage == "http_request":
                trace = details or {}
                status = "success" if trace.get("响应成功", trace.get("请求结果") != "网络异常") else "warning"
                self._append_log_locked(record, stage, "接口请求与响应", status, trace)
                return
            record.stage = str(stage)
            record.progress = STAGE_PROGRESS.get(record.stage, record.progress)
            self._publish_locked(
                task_id,
                "task.stage",
                {
                    "stage": record.stage,
                    "status": record.status,
                    "progress": record.progress,
                },
            )
            labels = {
                "billing_profile": "生成账单资料",
                "eligibility_check": "开始检测试用资格",
                "eligibility_confirmed": "账号具备试用资格",
                "eligibility_skipped": "未执行试用资格检测",
                "checkout": "创建 Checkout",
                "checkout_created": "Checkout 创建成功",
                "checkout_update": "更新 Checkout 国家与促销",
                "checkout_update_result": "Checkout 国家与促销更新结果",
                "stripe_init": "初始化 Stripe",
                "elements_session": "获取支付方式与 Elements Session",
                "payment_method_validation": "校验目标支付方式是否可用",
                "oaics_payment_channels": "识别 OAICS 标准/自定义支付通道",
                "oaics_custom_method": "按目标支付方式读取 OAICS 自定义通道",
                "taxes": "计算税费与应付金额",
                "payment_confirmation": "创建支付方式并确认",
                "wallet_pre_confirm": "钱包协议 pre_confirm 校验通过",
                "paypal_promo_sync": "等待 PayPal 优惠金额同步",
                "redirect_resolution": "解析支付跳转链接",
                "result_summary": "汇总实际提链结果",
                "completed": "流程完成",
            }
            log_status = (
                "warning"
                if record.stage == "payment_method_validation"
                and (details or {}).get("校验通过") is False
                else "success"
            )
            self._append_log_locked(
                record,
                record.stage,
                labels.get(record.stage, record.stage),
                log_status,
                details or {},
            )
            log_context(component="task", task_id=task_id, stage=record.stage).info("task stage")

    def _finish_cancelled_locked(self, record: TaskRecord, detail: str = "") -> None:
        record.status = "cancelled"
        record.stage = "cancelled"
        record.error = redact_text(detail) if detail else None
        record.finished_at = utc_timestamp()
        self._publish_locked(
            record.task_id,
            "task.cancelled",
            {"status": record.status, "progress": record.progress},
        )
        log_context(component="task", task_id=record.task_id).info("task cancelled")

    def _publish_locked(self, task_id: str, event_type: str, data: dict[str, Any]) -> None:
        event = make_event(task_id, event_type, data)
        self._history.append(event)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # Preserve terminal events; transient logs/stages may be dropped.
                if event_type in {"task.succeeded", "task.failed", "task.cancelled"}:
                    try:
                        subscriber.get_nowait()
                        subscriber.put_nowait(event)
                    except queue.Empty:
                        pass

    @staticmethod
    def _proxy_preview(value: str) -> str:
        match = re.match(r"^(\w+://)(?:[^@/]+@)?([^/]+)", str(value or ""))
        return f"{match.group(1)}***@{match.group(2)}" if match and "@" in value else (f"{match.group(1)}{match.group(2)}" if match else "已配置")

    @staticmethod
    def _append_log_locked(record: TaskRecord, step: str, label: str, status: str, details: dict[str, Any]) -> None:
        record.logs.append({
            "timestamp": utc_timestamp(),
            "step": step,
            "label": label,
            "status": status,
            "details": details,
            "attempt": record.attempt,
        })
        if len(record.logs) > 300:
            del record.logs[:-300]

    def _snapshot_locked(self, record: TaskRecord, *, include_logs: bool = False) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "ok": True,
            "task_id": record.task_id,
            "status": record.status,
            "stage": record.stage,
            "progress": record.progress,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "account_email": record.account_email,
            "payment_method": record.config.payment_method,
            "billing_country": record.config.country,
            "attempt": record.attempt,
            "max_attempts": record.config.auto_retry_count + 1,
        }
        if record.session_kind:
            snapshot["session_kind"] = record.session_kind
        if record.retry_of:
            snapshot["retry_of"] = record.retry_of
        if record.status == "succeeded":
            snapshot["checkout_proxy"] = record.config.checkout_proxy
        if record.result is not None:
            snapshot["result"] = record.result
        if record.error:
            snapshot["error"] = record.error
        if record.status == "failed":
            snapshot["network_error"] = record.network_error
            snapshot["failure_stage"] = record.failure_stage
            snapshot["error_kind"] = record.error_kind
            snapshot["error_http_status"] = record.error_http_status
            snapshot["failure_category"] = record.failure_category
            snapshot["retryable"] = record.retryable
        if include_logs:
            snapshot["logs"] = list(record.logs)
        return snapshot

    def _cleanup_locked(self) -> None:
        cutoff = datetime_now() - timedelta(seconds=self._ttl)
        expired = []
        for task_id, record in self._tasks.items():
            if record.status in TERMINAL_STATES and record.finished_at:
                try:
                    finished = datetime.fromisoformat(record.finished_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if finished < cutoff:
                    expired.append(task_id)
        for task_id in expired:
            self._tasks.pop(task_id, None)

    @staticmethod
    def _secrets(config: ExtractionConfig) -> tuple[str, ...]:
        return (
            config.access_token,
            config.checkout_proxy,
            config.update_proxy,
            config.stripe_hcaptcha_token,
        )


def datetime_now() -> datetime:
    return datetime.now(timezone.utc)
