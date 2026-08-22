from __future__ import annotations

import asyncio
import hmac
import httpx
import json
import os
import queue
import re
from datetime import datetime, timezone
import base64
from contextlib import asynccontextmanager
from enum import IntEnum
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .errors import (
    LocalProxyUnavailableError,
    DuplicateResourceError,
    InsufficientEmailsError,
    MongoUnavailableError,
    ProxyCountryUnavailableError,
    ResourceNotFoundError,
    RoxyWorkspaceMissingError,
    RunConflictError,
    RunNotFoundError,
)
from .mongo_manager import MongoManager
from .payment_tools import (
    AccessTokenExtractInput,
    AccessTokenExtractResult,
    extract_access_tokens,
)
from .oai_payment_extractor.web.env import load_configured_env
from .payment_extractor_service import (
    PaymentExtractorBulkDelete,
    PaymentExtractorConcurrencyUpdate,
    PaymentExtractorProxyTest,
    PaymentExtractorProxySource,
    PaymentExtractorService,
    PaymentExtractorServiceError,
    PaymentExtractorTaskCreate,
    PaymentExtractorTaskRetry,
)
from .paypal_agreement_service import (
    PaypalAgreementService,
    PaypalAgreementServiceError,
)
from .sandbox_checkout import router as sandbox_checkout_router
from .protocol_registration_service import ProtocolRegistrationService
from .pipeline_service import (
    AccountPipelineService,
    HeroSmsSettingsUpdate,
    PipelineBatchInput,
    PipelineOtpInput,
    PipelinePaidExportInput,
    PipelinePaidExportStatusInput,
    PipelinePaidMailCheckInput,
    PipelinePaymentInput,
    PipelineServiceError,
    PipelineSettingsUpdate,
    SmsReceiverBatchInput,
    SmsReceiverRetryInput,
    SmsReceiverHeroSmsSettingsUpdate,
    SmsReceiverSettingsUpdate,
)
from .probe_store import MongoProbeStore
from .plan_check_service import AccountPlanCheckService
from .plan_check_service import proxy_url as pooled_proxy_url
from .global_promotion_service import GlobalPromotionCheckService
from .account_alive_service import AccountAliveCheckService
from .account_alive_scheduler import AccountAliveScheduler
from .account_rebind_service import AccountRebindError, AccountRebindService, _safe_error
from .mailbox_client import direct_mailbox_access_url
from .proxy_subscription_service import (
    ProxySubscriptionError,
    ProxySubscriptionService,
)
from .proxy_health_scheduler import ProxyHealthScheduler
from .resource_models import (
    AccountCreate,
    AccountExportInput,
    AccountRecord,
    AccountPlanCheckInput,
    AccountPlanCheckResult,
    AccountCheckoutTypeCheckInput,
    AccountCheckoutTypeCheckResult,
    AccountAliveCheckInput,
    AccountAliveCheckResult,
    BulkIdsInput,
    BrowserProbeRunCreate,
    DeleteResult,
    EmailExportInput,
    EmailRecord,
    HealthResponse,
    ImportResult,
    MockRunCreate,
    OverviewStats,
    Page,
    ProxyRecord,
    ProxyCountrySummary,
    ProxyGroupSummary,
    ProxyGroupUpdate,
    ProxyImportInput,
    ProxySubscriptionImportInput,
    ProxySubscriptionImportResult,
    ProxyTestInput,
    ProxyTestResult,
    ProxyUpdate,
    RawImportInput,
    RunState,
    TextExport,
    WorkerSnapshot,
)
from .resource_service import MongoResourceStore, ResourceService
from .run_log_store import CorruptRunLogError, RunLogFile, RunLogNotFoundError, RunLogStore, RunLogSummary
from .run_manager import RunManager
from .roxy_client import RoxyApiError
from .run_store import MongoRunStore, MongoRunWorkerStore
from .settings_store import (
    CorruptSettingsError,
    ExecutionSettings,
    ExecutionSettingsInput,
    SettingsStore,
)

PageNumber = Annotated[int, Query(ge=1)]
SearchQuery = Annotated[str, Query(max_length=320)]


class PageSizeOption(IntEnum):
    TEN = 10
    TWENTY = 20
    FIFTY = 50
    ONE_HUNDRED = 100


def _snake_key(value: str) -> str:
    result = str(value)
    result = result.replace("-", "_")
    result = re.sub(r"(?<!^)(?=[A-Z])", "_", result)
    return result.lower()


def _snakeize(value):
    if isinstance(value, dict):
        return {_snake_key(str(key)): _snakeize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snakeize(item) for item in value]
    return value


def _task_response(value: dict, *, snake: bool = False) -> dict:
    result = _snakeize(value) if snake else dict(value)
    task_id = str(value.get("task_id") or value.get("taskId") or "")
    if task_id:
        if snake:
            result["status_url"] = f"/api/tasks/{task_id}"
            result["websocket_url"] = "/ws/tasks"
        else:
            result["statusUrl"] = f"/api/payment-extractor/tasks/{task_id}"
            result["websocketUrl"] = "/ws/tasks"
    return result


def _web_password() -> str:
    return os.getenv("OPLL_WEB_PASSWORD", "")


def _password_matches(request: Request, password: str) -> bool:
    if not password:
        return True
    supplied = request.headers.get("X-Workbench-Password", "")
    return hmac.compare_digest(supplied, password)


def _paypal_agreement_error(exc: PaypalAgreementServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _rewrite_paypal_agreement_csp(value: str) -> str:
    if not value:
        return "frame-ancestors 'self'"
    rewritten = re.sub(r"frame-ancestors\s+[^;]+", "frame-ancestors 'self'", value, flags=re.IGNORECASE)
    if rewritten == value and "frame-ancestors" not in rewritten.lower():
        rewritten = f"{rewritten.rstrip(';')}; frame-ancestors 'self'"
    return rewritten


def create_app(
    settings_path: Path | None = None,
    log_dir: Path | None = None,
    mongo_manager: MongoManager | None = None,
    payment_extractor_service: PaymentExtractorService | None = None,
    paypal_agreement_service: PaypalAgreementService | None = None,
    pipeline_service: AccountPipelineService | None = None,
) -> FastAPI:
    load_configured_env()
    settings_store = SettingsStore(settings_path) if settings_path else SettingsStore()
    run_log_store = RunLogStore(log_dir) if log_dir else RunLogStore()
    mongo = mongo_manager or MongoManager()
    resource_store = MongoResourceStore(mongo)
    resource_service = ResourceService(resource_store)
    proxy_subscription_service = ProxySubscriptionService(resource_service)
    proxy_health_scheduler = ProxyHealthScheduler(proxy_subscription_service)
    probe_store = MongoProbeStore(mongo)
    plan_check_service = AccountPlanCheckService(resource_store, probe_store)
    global_promotion_service = GlobalPromotionCheckService(resource_store, probe_store)
    alive_check_service = AccountAliveCheckService(resource_store, probe_store)
    alive_check_scheduler = AccountAliveScheduler(alive_check_service, resource_store)
    extractor_service = payment_extractor_service or PaymentExtractorService()
    agreement_service = paypal_agreement_service or PaypalAgreementService()
    account_pipeline = pipeline_service or AccountPipelineService(
        resource_store,
        extractor_service,
        agreement_service,
    )
    run_store = MongoRunStore(mongo)
    worker_store = MongoRunWorkerStore(mongo)
    run_manager = RunManager(
        mongo,
        resource_store,
        run_store,
        run_log_store,
        settings_store,
        probe_store=probe_store,
        worker_store=worker_store,
    )
    mongo.add_reconnect_callback(run_manager.recover)
    mongo.add_reconnect_callback(probe_store.ensure_indexes)
    mongo.add_reconnect_callback(account_pipeline.ensure_indexes)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        run_log_store.prune_terminal_runs()
        await mongo.start()
        await account_pipeline.start()
        await proxy_health_scheduler.start()
        await global_promotion_service.start()
        await alive_check_scheduler.start()
        try:
            yield
        finally:
            await alive_check_scheduler.stop()
            await global_promotion_service.stop()
            await proxy_health_scheduler.stop()
            await run_manager.shutdown()
            await account_pipeline.stop()
            extractor_service.close()
            agreement_service.close()
            await mongo.stop()

    app = FastAPI(
        title="AutoRegister Local Control Service",
        version="0.4.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(sandbox_checkout_router)

    def _protocol_mailbox_line(item) -> str:
        """Convert an email-pool record without changing legacy URL lines."""
        email = str(item.email or "").strip()
        access = str(item.accessUrl or "").strip()
        kind = str(getattr(item, "mailboxKind", "url") or "url").strip().lower()
        password = str(getattr(item, "mailboxPassword", "") or "").strip()
        if kind in {"cfworker", "cf_worker"}:
            return f"cfworker://{email}"
        if kind in {"gmail", "gmail_imap"} and password:
            return f"gmail://{email}---{password}"
        # Keep the existing mail.com/capability URL contract exactly as-is.
        return f"{email}----{access}"

    async def import_protocol_result(data: dict):
        email = str(data.get("email") or "").strip().lower()
        if not email:
            return
        try:
            mailbox = data.get("mailbox") or {}
            plan_type = str(data.get("plan_type") or "free").lower()
            account_type = plan_type if plan_type in {"free", "plus"} else "free"
            account = await resource_service.create_account(AccountCreate(
                email=email,
                chatgptPassword=str(data.get("password") or "protocol-managed"),
                totpSecret=str(data.get("totp_secret") or "not-configured"),
                emailAccessUrl=str(mailbox.get("token") or mailbox.get("access_url") or "protocol-managed"),
                accountType=account_type,
                registrationCountry=(str(data.get("registration_country") or "").strip().upper() or None),
            ))
            token = str(data.get("access_token") or "").strip()
            if token:
                # Access tokens are normally JWTs, but keep import resilient to
                # opaque tokens and malformed/stale session artifacts.
                parts = token.split(".")
                if len(parts) >= 2:
                    try:
                        padded = parts[1] + "=" * (-len(parts[1]) % 4)
                        claims = json.loads(base64.urlsafe_b64decode(padded).decode())
                        exp = int(claims.get("exp") or 0)
                        if exp > 0:
                            await resource_service.store_account_access_token(
                                account.id, token, datetime.fromtimestamp(exp, timezone.utc)
                            )
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                        pass
        except DuplicateResourceError:
            return
        except Exception:
            return

    app.state.protocol_registration = ProtocolRegistrationService(Path(__file__).resolve().parent, import_protocol_result)

    @app.middleware("http")
    async def payment_workbench_auth(request: Request, call_next):
        # Keep the local service open by default, while matching the reference
        # workbench's header gate whenever OPLL_WEB_PASSWORD is configured.
        # Registration/resource APIs are deliberately outside this namespace.
        path = request.url.path
        protected = (
            path == "/api/defaults"
            or path.startswith("/api/tasks")
            or path in {"/api/proxy/source", "/api/proxy/test"}
            or path.startswith("/api/payment-extractor")
            or path.startswith("/api/pipeline")
            or path.startswith("/api/protocol-registration")
        )
        if protected and not _password_matches(request, _web_password()):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"ok": False, "error": "unauthorized"},
            )
        return await call_next(request)

    app.state.settings_store = settings_store
    app.state.run_log_store = run_log_store
    app.state.mongo_manager = mongo
    app.state.resource_store = resource_store
    app.state.resource_service = resource_service
    app.state.proxy_subscription_service = proxy_subscription_service
    app.state.proxy_health_scheduler = proxy_health_scheduler
    app.state.probe_store = probe_store
    app.state.plan_check_service = plan_check_service
    app.state.alive_check_service = alive_check_service
    app.state.alive_check_scheduler = alive_check_scheduler
    app.state.payment_extractor_service = extractor_service
    app.state.paypal_agreement_service = agreement_service
    app.state.account_pipeline = account_pipeline
    app.state.run_store = run_store
    app.state.worker_store = worker_store
    app.state.run_manager = run_manager
    app.state.account_rebind_proxy = ""
    app.state.account_rebind_tasks = {}
    app.state.account_rebind_logs = []
    app.state.account_rebind_concurrency = 2
    app.state.account_rebind_semaphore = asyncio.Semaphore(2)
    app.state.account_rebind_jobs = {}
    app.state.account_rebind_deleted_tasks = set()

    @app.exception_handler(MongoUnavailableError)
    async def mongodb_unavailable_handler(
        _request: Request, exc: MongoUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    "code": "mongodb_unavailable",
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(DuplicateResourceError)
    async def duplicate_handler(
        _request: Request, exc: DuplicateResourceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": {"code": "duplicate_resource", "message": str(exc)}},
        )

    @app.exception_handler(ResourceNotFoundError)
    async def missing_resource_handler(
        _request: Request, exc: ResourceNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": {"code": "resource_not_found", "message": str(exc)}},
        )

    @app.exception_handler(PipelineServiceError)
    async def pipeline_error_handler(
        _request: Request, exc: PipelineServiceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        mongo_health = mongo.health()
        return HealthResponse(
            status="ok" if mongo_health.status == "online" else "degraded",
            mongodb=mongo_health,
        )

    @app.post("/api/protocol-registration/start")
    async def protocol_registration_start(request: Request) -> dict:
        payload = await request.json()
        mailbox_file = str(payload.get("mailboxFile") or "").strip()
        proxy = str(payload.get("proxy") or "").strip()
        requested_proxy_pool = payload.get("proxyPool") or payload.get("proxy_pool") or []
        if isinstance(requested_proxy_pool, str):
            requested_proxy_pool = [item.strip() for item in requested_proxy_pool.replace(",", "\n").splitlines() if item.strip()]
        elif not isinstance(requested_proxy_pool, list):
            requested_proxy_pool = []
        requested_country = str(payload.get("country") or "").strip().upper()
        requested_group = str(payload.get("proxyGroup") or "").strip()
        # Reuse the existing account-pool mailbox and proxy stores when the
        # caller does not provide an explicit sidecar file/proxy.
        if not mailbox_file:
            mailbox_records = await resource_store.emails_for_export(None)
            mailbox_path = Path(__file__).resolve().parent.parent.parent / "protocol-registration" / "runtime" / "existing-mailboxes.txt"
            mailbox_path.parent.mkdir(parents=True, exist_ok=True)
            mailbox_path.write_text(
                "\n".join(_protocol_mailbox_line(item) for item in mailbox_records),
                encoding="utf-8",
            )
            mailbox_file = str(mailbox_path) if mailbox_records else ""
        if not proxy and not requested_proxy_pool:
            candidates = await probe_store.all_eligible_proxy_candidates()
            if requested_country:
                candidates = [item for item in candidates if item.country == requested_country]
            if requested_group:
                candidates = [item for item in candidates if item.group == requested_group]
            proxy_values = []
            for item in candidates:
                auth = f"{item.username}:{item.password}@" if item.username else ""
                proxy_values.append(f"{item.scheme}://{auth}{item.host}:{item.port}")
            requested_proxy_pool = proxy_values
            proxy = proxy_values[0] if proxy_values else ""
        return await request.app.state.protocol_registration.start(
            count=payload.get("count", 1), workers=payload.get("workers", 1),
            mailbox_file=mailbox_file, proxy=proxy, proxy_pool=requested_proxy_pool,
        )

    @app.get("/api/protocol-registration/{job_id}")
    async def protocol_registration_status(job_id: str, request: Request) -> dict:
        state = request.app.state.protocol_registration.get(job_id)
        if state is None:
            raise HTTPException(status_code=404, detail="协议注册任务不存在")
        return state

    @app.post("/api/protocol-registration/import")
    async def protocol_registration_import(request: Request) -> dict:
        """Import one canonical protocol session into the normal account pool."""
        payload = await request.json()
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise HTTPException(status_code=422, detail="协议结果缺少邮箱")
        account = await resource_service.create_account(AccountCreate(
            email=email,
            chatgptPassword=str(payload.get("password") or "protocol-managed"),
            totpSecret=str(payload.get("totp_secret") or "protocol-managed"),
            emailAccessUrl=str(payload.get("email_access_url") or payload.get("mailbox_url") or "protocol-managed"),
            accountType=str(payload.get("account_type") or "free"),
            registrationCountry=str(payload.get("registration_country") or "") or None,
        ))
        token = str(payload.get("access_token") or "").strip()
        if token:
            try:
                claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==").decode())
                expires_at = datetime.fromtimestamp(int(claims.get("exp")), timezone.utc)
                await resource_service.store_account_access_token(account.id, token, expires_at)
            except Exception:
                pass
        return {"ok": True, "account": account.model_dump(mode="json")}

    @app.get("/api/paypal-agreement/status")
    async def paypal_agreement_status(request: Request) -> dict:
        return await asyncio.to_thread(request.app.state.paypal_agreement_service.status)

    @app.post("/api/paypal-agreement/start")
    async def paypal_agreement_start(request: Request) -> dict:
        try:
            return await asyncio.to_thread(request.app.state.paypal_agreement_service.start)
        except PaypalAgreementServiceError as exc:
            raise _paypal_agreement_error(exc) from exc

    @app.post("/api/paypal-agreement/stop")
    async def paypal_agreement_stop(request: Request) -> dict:
        return await asyncio.to_thread(request.app.state.paypal_agreement_service.stop)

    @app.get("/api/pipeline/settings")
    async def pipeline_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.settings()

    @app.put("/api/pipeline/settings")
    async def update_pipeline_settings(
        payload: PipelineSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_settings(payload)

    @app.get("/api/herosms/settings")
    async def hero_sms_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.hero_sms_settings()

    @app.put("/api/herosms/settings")
    async def update_hero_sms_settings(
        payload: HeroSmsSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_hero_sms_settings(payload)

    @app.get("/api/herosms/countries")
    async def hero_sms_countries(request: Request) -> list[dict]:
        return await request.app.state.account_pipeline.hero_sms_countries()

    @app.post("/api/herosms/test")
    async def test_hero_sms(request: Request) -> dict:
        return await request.app.state.account_pipeline.hero_sms_test()

    @app.get("/api/pipeline")
    async def list_pipeline_items(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, alias="pageSize", ge=10, le=100),
        stage: str = Query(default="", max_length=64),
        q: str = Query(default="", max_length=320),
        export_state: str = Query(default="all", alias="exportState", pattern="^(all|exported|unexported)$"),
        settlement_state: str = Query(
            default="all",
            alias="settlementState",
            pattern="^(all|waiting|confirmed|review|failed)$",
        ),
        receiver_state: str = Query(
            default="all",
            alias="receiverState",
            pattern="^(all|verified|unverified|failed|pending)$",
        ),
    ) -> dict:
        return await request.app.state.account_pipeline.list_items(
            page=page,
            page_size=page_size,
            stage=stage,
            query=q,
            export_state=export_state,
            settlement_state=settlement_state,
            receiver_state=receiver_state,
        )

    @app.get("/api/pipeline/{item_id}/logs")
    async def pipeline_item_logs(item_id: str, request: Request) -> dict:
        return await request.app.state.account_pipeline.item_logs(item_id)

    @app.get("/api/pipeline/paid/stats")
    async def pipeline_paid_stats(
        request: Request,
        days: int = Query(default=14, ge=7, le=31),
    ) -> dict:
        return await request.app.state.account_pipeline.paid_stats(days=days)

    @app.post("/api/pipeline/paid/export")
    async def export_paid_pipeline_accounts(
        payload: PipelinePaidExportInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.export_paid(payload)

    @app.post("/api/pipeline/paid/export-status")
    async def update_paid_pipeline_export_status(
        payload: PipelinePaidExportStatusInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.mark_paid_export_status(payload)

    @app.post("/api/pipeline/paid/mail-check")
    async def check_paid_pipeline_mail(
        payload: PipelinePaidMailCheckInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.check_paid_mail(payload)

    @app.get("/api/pipeline/sms-receiver/settings")
    async def sms_receiver_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.sms_receiver_settings()

    @app.put("/api/pipeline/sms-receiver/settings")
    async def update_sms_receiver_settings(
        payload: SmsReceiverSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_sms_receiver_settings(payload)

    @app.post("/api/pipeline/sms-receiver/test")
    async def test_sms_receiver(request: Request) -> dict:
        return await request.app.state.account_pipeline.test_sms_receiver()

    @app.get("/api/pipeline/sms-receiver/herosms")
    async def sms_receiver_hero_sms_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.sms_receiver_hero_sms_settings()

    @app.put("/api/pipeline/sms-receiver/herosms")
    async def update_sms_receiver_hero_sms_settings(
        payload: SmsReceiverHeroSmsSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_sms_receiver_hero_sms_settings(
            payload
        )

    @app.get("/api/pipeline/sms-receiver/herosms/catalog")
    async def sms_receiver_hero_sms_catalog(request: Request) -> dict:
        return await request.app.state.account_pipeline.sms_receiver_hero_sms_catalog()

    @app.post("/api/pipeline/paid/sms-receiver/submit")
    async def submit_paid_to_sms_receiver(
        payload: SmsReceiverBatchInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.submit_paid_to_sms_receiver(payload)

    @app.post("/api/pipeline/paid/sms-receiver/status")
    async def refresh_sms_receiver_status(
        payload: SmsReceiverBatchInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.refresh_sms_receiver_status(payload)

    @app.post("/api/pipeline/paid/sms-receiver/retry")
    async def retry_paid_sms_receiver(
        payload: SmsReceiverRetryInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.queue_sms_receiver_retry(payload)

    @app.post("/api/pipeline/sync")
    async def sync_pipeline(request: Request) -> dict:
        result = await request.app.state.account_pipeline.sync_eligible()
        await request.app.state.account_pipeline.tick()
        return result

    @app.post("/api/pipeline/extract", status_code=202)
    async def start_pipeline_extractions(
        payload: PipelineBatchInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.start_extractions(payload.ids)

    @app.post("/api/pipeline/herosms/test")
    async def test_pipeline_hero_sms(request: Request) -> dict:
        return await request.app.state.account_pipeline.hero_sms_test()

    @app.post("/api/pipeline/{item_id}/payment", status_code=202)
    async def start_pipeline_payment(
        item_id: str,
        payload: PipelinePaymentInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.start_payment(item_id, payload)

    @app.post("/api/pipeline/{item_id}/otp")
    async def submit_pipeline_otp(
        item_id: str,
        payload: PipelineOtpInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.submit_otp(item_id, payload)

    @app.post("/api/pipeline/{item_id}/retry-extraction")
    async def retry_pipeline_extraction(item_id: str, request: Request) -> dict:
        return await request.app.state.account_pipeline.reset_stage(item_id, "extraction")

    @app.post("/api/pipeline/{item_id}/retry-payment")
    async def retry_pipeline_payment(item_id: str, request: Request) -> dict:
        return await request.app.state.account_pipeline.reset_stage(item_id, "payment")

    @app.delete("/api/pipeline/{item_id}")
    async def delete_pipeline_item(item_id: str, request: Request) -> dict:
        return {"deleted": await request.app.state.account_pipeline.delete(item_id)}

    @app.get("/api/paypal-agreement/source")
    async def paypal_agreement_source(request: Request) -> dict:
        service: PaypalAgreementService = request.app.state.paypal_agreement_service
        return {
            "service": "paypal-agreement-protocol",
            "sourceCommit": service.source_commit,
            "uiPath": service.ui_path,
            "isolated": True,
        }

    @app.get("/paypal-pay")
    async def paypal_agreement_root_redirect() -> RedirectResponse:
        return RedirectResponse("/paypal-pay/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.api_route(
        "/paypal-pay/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def paypal_agreement_proxy(path: str, request: Request) -> Response:
        service: PaypalAgreementService = request.app.state.paypal_agreement_service
        try:
            await asyncio.to_thread(service.start)
        except PaypalAgreementServiceError as exc:
            raise _paypal_agreement_error(exc) from exc

        upstream_url = f"{service.base_url}/{path}"
        if request.url.query:
            upstream_url = f"{upstream_url}?{request.url.query}"
        forward_headers: dict[str, str] = {}
        for header_name in ("accept", "accept-language", "cache-control", "content-type", "cookie", "user-agent"):
            value = request.headers.get(header_name)
            if value:
                forward_headers[header_name] = value
        # The sidecar validates same-origin POSTs against its own Host.  The
        # reverse proxy is the trusted local boundary, so never forward the
        # browser's Origin/Referer values from the outer app.
        forward_headers["host"] = f"{service.host}:{service.port}"
        body = await request.body()
        hero_context: dict | None = None
        if request.method == "POST" and path.rstrip("/") == "api/jobs":
            try:
                incoming_payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                incoming_payload = None
            if isinstance(incoming_payload, dict):
                incoming_payload, hero_context = await request.app.state.account_pipeline.prepare_agreement_hero_sms(
                    incoming_payload
                )
                body = json.dumps(incoming_payload, ensure_ascii=False).encode("utf-8")
                forward_headers["content-type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False) as client:
                upstream = await client.request(
                    request.method,
                    upstream_url,
                    headers=forward_headers,
                    content=body,
                )
        except httpx.HTTPError as exc:
            await request.app.state.account_pipeline.cancel_prepared_agreement_hero_sms(
                hero_context
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "paypal_agreement_proxy_error", "message": str(exc)},
            ) from exc

        if hero_context is not None:
            if upstream.status_code >= 400:
                await request.app.state.account_pipeline.cancel_prepared_agreement_hero_sms(
                    hero_context
                )
            else:
                try:
                    upstream_payload = upstream.json()
                except ValueError:
                    upstream_payload = {}
                job = upstream_payload.get("job") if isinstance(upstream_payload, dict) else None
                job_id = str(job.get("id") or "") if isinstance(job, dict) else ""
                cookie = request.headers.get("cookie", "")
                if not cookie:
                    for value in upstream.headers.get_list("set-cookie"):
                        match = re.search(r"paypal_web_device_id=([^;]+)", value)
                        if match:
                            cookie = f"paypal_web_device_id={match.group(1)}"
                            break
                if job_id:
                    request.app.state.account_pipeline.track_agreement_hero_sms(
                        job_id, cookie, hero_context
                    )
                else:
                    await request.app.state.account_pipeline.cancel_prepared_agreement_hero_sms(
                        hero_context
                    )

        response_headers: dict[str, str] = {}
        for header_name in ("content-type", "cache-control", "content-disposition", "etag", "last-modified"):
            value = upstream.headers.get(header_name)
            if value:
                response_headers[header_name] = value
        if "content-security-policy" in upstream.headers:
            response_headers["content-security-policy"] = _rewrite_paypal_agreement_csp(
                upstream.headers["content-security-policy"]
            )
        location = upstream.headers.get("location")
        if location:
            response_headers["location"] = location.replace(service.base_url, "", 1) or "/paypal-pay/"
        result = Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )
        for cookie in upstream.headers.get_list("set-cookie"):
            # Keep the sidecar's device cookie scoped to its integration path.
            scoped_cookie = re.sub(r"(?i)path=/($|;)", r"Path=/paypal-pay/\1", cookie, count=1)
            result.headers.append("set-cookie", scoped_cookie)
        # The source's standalone X-Frame-Options header is deliberately not
        # copied; framing is allowed only through this same-origin local proxy.
        return result

    @app.post(
        "/api/tools/access-tokens/extract",
        response_model=AccessTokenExtractResult,
    )
    def extract_access_token_payload(
        payload: AccessTokenExtractInput,
    ) -> AccessTokenExtractResult:
        return extract_access_tokens(payload.rawText)

    def extractor_error(exc: PaymentExtractorServiceError) -> HTTPException:
        return HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        )

    @app.get("/api/payment-extractor/defaults")
    def payment_extractor_defaults(request: Request) -> dict:
        return request.app.state.payment_extractor_service.options()

    @app.get("/api/payment-extractor/accounts")
    async def payment_extractor_accounts() -> dict:
        mongo.require_online()
        return {"ok": True, "items": await resource_store.payment_extractor_accounts()}

    @app.get("/api/payment-extractor/proxy-pool")
    async def payment_extractor_proxy_pool(
        country: Annotated[str, Query(max_length=2)] = "",
        group: Annotated[str, Query(max_length=128)] = "",
    ) -> dict:
        mongo.require_online()
        proxies = await resource_store.payment_extractor_proxy_pool(country, group)
        return {"ok": True, "proxies": proxies, "count": len(proxies)}

    @app.put("/api/payment-extractor/concurrency")
    def payment_extractor_concurrency(
        payload: PaymentExtractorConcurrencyUpdate,
        request: Request,
    ) -> dict:
        try:
            return request.app.state.payment_extractor_service.set_concurrency(payload)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/proxy-test")
    async def payment_extractor_proxy_test(
        payload: PaymentExtractorProxyTest,
        request: Request,
    ) -> dict:
        try:
            return await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_test,
                payload,
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/proxy-source")
    async def payment_extractor_proxy_source(
        payload: PaymentExtractorProxySource,
        request: Request,
    ) -> dict:
        try:
            return await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_source,
                payload,
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks", status_code=202)
    async def create_payment_extractor_task(
        payload: PaymentExtractorTaskCreate,
        request: Request,
    ) -> dict:
        try:
            if payload.accountId and not payload.accessToken:
                token = await resource_store.payment_extractor_access_token(payload.accountId)
                payload = payload.model_copy(update={"accessToken": token})
            return _task_response(
                request.app.state.payment_extractor_service.create(payload)
            )
        except ResourceNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.get("/api/payment-extractor/tasks")
    def list_payment_extractor_tasks(request: Request) -> dict:
        return {
            "ok": True,
            "tasks": request.app.state.payment_extractor_service.list(),
        }

    # Static task collection actions must be registered before /{task_id};
    # otherwise FastAPI/Starlette can treat "bulk-cancel" as a task id and
    # return 405 before reaching the intended POST route.
    @app.post("/api/payment-extractor/tasks/bulk-cancel")
    def cancel_all_payment_extractor_tasks(request: Request) -> dict:
        return request.app.state.payment_extractor_service.cancel_all()

    @app.post("/api/payment-extractor/tasks/bulk-delete")
    def bulk_delete_payment_extractor_tasks(
        payload: PaymentExtractorBulkDelete,
        request: Request,
    ) -> dict:
        return request.app.state.payment_extractor_service.bulk_delete(payload)

    @app.get("/api/payment-extractor/tasks/{task_id}")
    def get_payment_extractor_task(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.get(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/{task_id}/cancel")
    def cancel_payment_extractor_task(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.cancel(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/{task_id}/retry", status_code=202)
    def retry_payment_extractor_task(
        task_id: str,
        payload: PaymentExtractorTaskRetry,
        request: Request,
    ) -> dict:
        try:
            return _task_response(
                request.app.state.payment_extractor_service.retry(task_id, payload)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/{task_id}/resolve-paypal")
    def resolve_payment_extractor_paypal(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.resolve_paypal(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.delete("/api/payment-extractor/tasks/{task_id}")
    def delete_payment_extractor_task(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.delete(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    # Compatibility routes matching the standalone extractor README.  The
    # existing /api/payment-extractor/* routes above remain unchanged for the
    # AutoRegister frontend.
    @app.get("/api/defaults")
    def extractor_defaults_compat(request: Request) -> dict:
        return _snakeize(request.app.state.payment_extractor_service.options())

    @app.get("/api/proxy/source")
    async def extractor_proxy_source_compat(
        request: Request, url: str = Query(default="", max_length=4096)
    ) -> dict:
        try:
            result = await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_source,
                PaymentExtractorProxySource(url=url),
            )
            return _snakeize(result)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/proxy/test")
    async def extractor_proxy_test_compat(
        payload: PaymentExtractorProxyTest, request: Request
    ) -> dict:
        try:
            result = await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_test, payload
            )
            return _snakeize(result)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.get("/api/tasks")
    def extractor_tasks_compat(request: Request) -> dict:
        return {
            "ok": True,
            "tasks": _snakeize(request.app.state.payment_extractor_service.list()),
        }

    @app.post("/api/tasks", status_code=202)
    def extractor_create_task_compat(
        payload: PaymentExtractorTaskCreate, request: Request
    ) -> dict:
        try:
            result = request.app.state.payment_extractor_service.create(payload)
            return _task_response(result, snake=True)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.get("/api/tasks/{task_id}")
    def extractor_get_task_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(request.app.state.payment_extractor_service.get(task_id))
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/{task_id}/cancel")
    def extractor_cancel_task_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(request.app.state.payment_extractor_service.cancel(task_id))
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def extractor_retry_task_compat(
        task_id: str, payload: PaymentExtractorTaskRetry, request: Request
    ) -> dict:
        try:
            result = request.app.state.payment_extractor_service.retry(task_id, payload)
            return _task_response(result, snake=True)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/{task_id}/resolve-paypal")
    def extractor_resolve_paypal_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(
                request.app.state.payment_extractor_service.resolve_paypal(task_id)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.delete("/api/tasks/{task_id}")
    def extractor_delete_task_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(request.app.state.payment_extractor_service.delete(task_id))
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/bulk-delete")
    def extractor_bulk_delete_compat(
        payload: PaymentExtractorBulkDelete, request: Request
    ) -> dict:
        try:
            return _snakeize(
                request.app.state.payment_extractor_service.bulk_delete(payload)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.websocket("/ws/tasks")
    async def extractor_task_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        password = _web_password()
        service = websocket.app.state.payment_extractor_service
        try:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            supplied = ""
            if isinstance(message, dict):
                supplied = str(message.get("password") or "")
            valid_auth = (
                isinstance(message, dict)
                and message.get("type") == "auth"
                and hmac.compare_digest(supplied, password)
            )
            if not valid_auth:
                await websocket.send_json(
                    {"type": "auth.failed", "data": {"error": "unauthorized"}}
                )
                await websocket.close(code=1008)
                return
            await websocket.send_json({"type": "auth.ok"})
        except Exception:
            try:
                await websocket.close(code=1008)
            except Exception:
                pass
            return

        subscriber = None
        try:
            history, subscriber = service.subscribe()
            for event in history:
                await websocket.send_json(_snakeize(service.public_event(event)))
            loop = asyncio.get_running_loop()
            last_ping = loop.time()
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 1.0)
                except queue.Empty:
                    if loop.time() - last_ping >= 15:
                        await websocket.send_json(
                            {"type": "task.ping", "task_id": "", "timestamp": "", "data": {}}
                        )
                        last_ping = loop.time()
                    continue
                await websocket.send_json(_snakeize(service.public_event(event)))
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            if subscriber is not None:
                try:
                    service.unsubscribe(subscriber)
                except Exception:
                    pass

    @app.get("/api/settings/execution", response_model=ExecutionSettings)
    def get_execution_settings(request: Request) -> ExecutionSettings:
        try:
            return request.app.state.settings_store.load_public()
        except CorruptSettingsError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_corrupted", "message": str(exc)},
            ) from exc

    @app.put("/api/settings/execution", response_model=ExecutionSettings)
    def put_execution_settings(
        payload: ExecutionSettingsInput,
        request: Request,
    ) -> ExecutionSettings:
        try:
            stored = request.app.state.settings_store.save(payload)
            return ExecutionSettings.from_stored(stored)
        except CorruptSettingsError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_corrupted", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_write_failed", "message": "无法原子保存配置文件"},
            ) from exc

    @app.get("/api/accounts", response_model=Page[AccountRecord])
    async def list_accounts(
        page: PageNumber = 1,
        page_size: PageSizeOption = Query(PageSizeOption.TEN, alias="pageSize"),
        q: SearchQuery = "",
        promotion: Annotated[str, Query(max_length=32)] = "",
        country: Annotated[str, Query(max_length=2)] = "",
        alive: Annotated[str, Query(pattern="^(|alive|dead|unknown|unchecked)$")] = "",
        global_promotion: Annotated[
            str, Query(alias="globalPromotion", pattern="^(|eligible|ineligible|pending|failed)$")
        ] = "",
        rebind: Annotated[str, Query(pattern="^(|ready|rebound)$")] = "",
        rebind_country: Annotated[str, Query(alias="rebindCountry", max_length=2)] = "",
    ) -> Page[AccountRecord]:
        mongo.require_online()
        return await resource_store.list_accounts(
            page, int(page_size), q, promotion, country, alive, global_promotion, rebind, rebind_country
        )  # type: ignore[arg-type]

    rebind_step_labels = {
        "task.created": "任务已创建",
        "task.recovered": "恢复失败任务",
        "task.reconciled": "清理已成功账号",
        "proxy.preflight": "代理池预检",
        "proxy.preflight_failed": "代理池预检失败",
        "proxy.selected": "代理范围已确认",
        "proxy.acquiring": "正在领取代理",
        "proxy.retry": "代理失败，正在轮换",
        "proxy.acquired": "代理连接正常",
        "mailbox.reserve": "正在分配邮箱",
        "mailbox.reserved": "换绑邮箱已分配",
        "mailbox.failed": "换绑邮箱分配失败",
        "workflow.dispatched": "执行器已启动",
        "workflow.start": "账号开始执行",
        "login.csrf": "初始化登录会话",
        "login.password": "验证账号密码",
        "login.email_fallback": "切换邮箱验证码登录",
        "login.email": "使用邮箱验证码登录",
        "login.mfa": "验证双重认证",
        "email.wait": "等待邮箱验证码",
        "login.success": "原账号登录成功",
        "rebind.eligibility": "检查换绑资格",
        "rebind.begin": "发送新邮箱验证码",
        "rebind.verify": "确认新邮箱验证码",
        "confirm.login": "使用新邮箱重新登录",
        "rebind.changed": "新邮箱已写入账号池",
        "token.refresh": "重新获取访问令牌",
        "token.refresh_success": "访问令牌已更新",
        "promotion.check": "正在检测优惠资格",
        "promotion.success": "优惠资格检测完成",
        "promotion.failed": "优惠资格检测失败",
        "complete": "换绑确认完成",
        "mailbox.consume_repaired": "修复邮箱占用状态",
        "workflow.success": "换绑成功",
        "workflow.failed": "换绑失败",
        "concurrency.updated": "并发设置已更新",
    }

    def _rebind_step_label(step: str) -> str:
        return rebind_step_labels.get(step, step or "任务状态")

    def _rebind_error_message(code: str) -> str:
        if code.startswith("mailbox_network_error:"):
            return "邮箱接码服务连接失败"
        if code.startswith("network_error:"):
            return "网络连接失败"
        if code.startswith("password_verify_http_"):
            return "账号密码验证失败"
        if code.startswith("email_change_ineligible:"):
            return "当前账号不符合邮箱换绑资格"
        if code.startswith("email_change_begin_http_"):
            return "发送新邮箱验证码失败"
        if code.startswith("email_change_verify_http_"):
            return "新邮箱验证码确认失败"
        if code.startswith("auth_session_http_"):
            return "获取登录会话和 AccessToken 失败"
        if code == "auth_session_token_missing":
            return "登录回调完成，但 AccessToken 会话尚未建立"
        return {
            "cloudflare_challenge_required": "代理出口触发 Cloudflare 验证",
            "signin_failed_http_403": "登录入口返回 403（通常为 Cloudflare 验证）",
            "no_eligible_proxy": "所选代理池没有可用代理",
            "email_code_timeout": "等待邮箱验证码超时",
            "account_login_credentials_missing": "账号缺少密码和邮箱接码地址",
            "mfa_credentials_missing": "账号缺少可用的双重认证凭据",
            "reserved_mailbox_not_found": "已分配的换绑邮箱不存在或占用失效",
            "reserved_mailbox_credentials_missing": "换绑邮箱缺少接码地址",
            "rebind_confirmation_email_mismatch": "换绑后登录邮箱校验不一致",
            "auth_token_refresh_required": "邮箱已换绑，等待重新获取 AccessToken",
        }.get(code, "执行失败")

    def _rebind_log(
        level: str,
        message: str,
        *,
        task_id: str = "",
        account_id: str = "",
        step: str = "",
        percent: int | None = None,
    ) -> None:
        masked_message = re.sub(
            r"(?i)([a-z0-9._%+-]{1,2})[a-z0-9._%+-]*(@[a-z0-9.-]+\.[a-z]{2,})",
            r"\1***\2",
            message,
        )
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": _safe_error(masked_message),
        }
        if task_id:
            entry["taskId"] = task_id
        if account_id:
            entry["accountId"] = account_id
        if step:
            entry["step"] = step
            entry["stepLabel"] = _rebind_step_label(step)
        if percent is not None:
            entry["percent"] = max(0, min(100, int(percent)))
        app.state.account_rebind_logs.append(entry)
        if len(app.state.account_rebind_logs) > 500:
            del app.state.account_rebind_logs[:-300]
        try:
            loop = asyncio.get_running_loop()

            async def persist_log() -> None:
                try:
                    await resource_store.append_rebind_log(entry)
                except Exception:
                    # Runtime logging must never interrupt an account workflow.
                    return

            loop.create_task(persist_log())
        except RuntimeError:
            pass

    def _masked_proxy(proxy: str) -> str:
        try:
            parsed = urlparse(proxy)
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{host}{port}" if parsed.scheme and host else "configured"
        except ValueError:
            return "configured"

    async def _acquire_rebind_proxy(
        selector: dict,
        owner: str,
        *,
        excluded_ids: set[str] | None = None,
    ):
        mode = str(selector.get("mode") or "")
        if mode == "custom":
            return None, str(selector.get("proxy") or "")
        if mode == "pool":
            lease = await probe_store.acquire_proxy(
                owner,
                excluded_ids=excluded_ids,
                lease_seconds=600,
                country=str(selector.get("country") or "") or None,
                group=str(selector.get("group") or "") or None,
            )
        else:
            lease = await probe_store.acquire_proxy_by_id(
                str(selector.get("proxyId") or "local7890"),
                owner,
                lease_seconds=600,
                country=str(selector.get("country") or "") or None,
            )
        return lease, pooled_proxy_url(lease) if lease is not None else ""

    async def _preflight_rebind_proxy(task_id: str, selector: dict) -> str:
        mode = str(selector.get("mode") or "")
        attempts = 1
        if mode == "pool":
            attempts = min(
                5,
                max(
                    1,
                    await probe_store.count_eligible_proxies(
                        str(selector.get("country") or "") or None,
                        str(selector.get("group") or "") or None,
                    ),
                ),
            )
        excluded: set[str] = set()
        last_error: AccountRebindError | None = None
        for attempt in range(attempts):
            owner = f"rebind-preflight:{task_id}:{attempt}"
            lease = None
            try:
                lease, proxy = await _acquire_rebind_proxy(
                    selector, owner, excluded_ids=excluded
                )
                if not proxy:
                    raise AccountRebindError("no_eligible_proxy", retryable=True)
                await asyncio.to_thread(AccountRebindService().probe_proxy, proxy)
                if lease is not None:
                    await probe_store.record_proxy_registration_success(lease.id)
                return _masked_proxy(proxy)
            except AccountRebindError as exc:
                last_error = exc
                if lease is not None:
                    excluded.add(lease.id)
                    if exc.code == "cloudflare_challenge_required":
                        await probe_store.record_proxy_registration_rejection(
                            lease.id,
                            code=exc.code,
                        )
                if not exc.retryable or mode != "pool":
                    break
            finally:
                if lease is not None:
                    await probe_store.release_proxy(lease.id, owner)
        raise last_error or AccountRebindError("no_eligible_proxy", retryable=True)

    async def _acquire_verified_rebind_proxy(
        task_id: str,
        account_id: str,
        selector: dict,
        owner: str,
    ):
        mode = str(selector.get("mode") or "")
        attempts = 1
        if mode == "pool":
            attempts = min(
                20,
                max(
                    1,
                    await probe_store.count_eligible_proxies(
                        str(selector.get("country") or "") or None,
                        str(selector.get("group") or "") or None,
                    ),
                ),
            )
        excluded: set[str] = set()
        last_error: AccountRebindError | None = None
        for attempt in range(1, attempts + 1):
            lease = None
            proxy = ""
            try:
                _rebind_log(
                    "INFO",
                    f"正在领取并验证第 {attempt}/{attempts} 条代理",
                    task_id=task_id,
                    account_id=account_id,
                    step="proxy.acquiring",
                    percent=7,
                )
                lease, proxy = await _acquire_rebind_proxy(
                    selector, owner, excluded_ids=excluded
                )
                if not proxy:
                    raise AccountRebindError("no_eligible_proxy", retryable=True)
                await asyncio.to_thread(AccountRebindService().probe_proxy, proxy)
                if lease is not None:
                    await probe_store.record_proxy_registration_success(lease.id)
                return lease, proxy, attempt
            except AccountRebindError as exc:
                last_error = exc
                if lease is not None:
                    excluded.add(lease.id)
                    if exc.retryable:
                        await probe_store.record_proxy_registration_rejection(
                            lease.id, code=exc.code, cooldown_seconds=600
                        )
                    await probe_store.release_proxy(lease.id, owner)
                _rebind_log(
                    "WARN",
                    f"第 {attempt}/{attempts} 条代理不可用（{_rebind_error_message(exc.code)}），正在切换下一条",
                    task_id=task_id,
                    account_id=account_id,
                    step="proxy.retry",
                    percent=7,
                )
                if mode != "pool" or not exc.retryable:
                    break
        raise last_error or AccountRebindError("no_eligible_proxy", retryable=True)

    def _refresh_rebind_task(task: dict) -> None:
        items = task.get("items", [])
        task["progress"] = int(
            sum(int(item.get("progress") or 0) for item in items) / max(1, len(items))
        )
        statuses = {str(item.get("status") or "") for item in items}
        if statuses & {"queued", "running"}:
            task["status"] = "running"
            task["message"] = "正在执行账号换绑"
        elif "pending" in statuses:
            task["status"] = "pending"
            task["message"] = "等待选择代理、邮箱类型并开始换绑"
        elif statuses == {"success"}:
            task["status"] = "success"
            task["message"] = "全部账号换绑成功，账号池和 AT 已更新"
        elif "success" in statuses:
            task["status"] = "partial_success"
            task["message"] = "部分账号换绑成功，请检查失败项"
        elif statuses <= {"cancelled", "released"}:
            task["status"] = "cancelled"
            task["message"] = "任务已取消"
        else:
            task["status"] = "failed"
            task["message"] = "账号换绑失败，请检查错误码"
        task["updatedAt"] = datetime.now(timezone.utc).isoformat()
        task_id = str(task.get("taskId") or "")
        if task_id and task_id not in app.state.account_rebind_deleted_tasks:
            try:
                async def persist_task() -> None:
                    try:
                        if task_id not in app.state.account_rebind_deleted_tasks:
                            await resource_store.save_rebind_task(task)
                    except Exception:
                        return

                asyncio.get_running_loop().create_task(persist_task())
            except RuntimeError:
                pass

    async def _load_durable_rebind_tasks() -> int:
        if app.state.account_rebind_tasks:
            return 0
        documents = await resource_store.list_rebind_tasks(500)
        loaded = 0
        for document in documents:
            task_id = str(document.get("taskId") or "")
            if not task_id:
                continue
            document.pop("updatedAt", None)
            interrupted = False
            for item in document.get("items", []):
                if item.get("status") in {"running", "queued"}:
                    interrupted = True
                    item.update(
                        status="failed",
                        retryable=True,
                        error="backend_restarted",
                        step="workflow.failed",
                        stepLabel=_rebind_step_label("workflow.failed"),
                        message="后端重启导致执行中断；原邮箱租约仍保留，可直接重试",
                    )
            if interrupted:
                document["status"] = "failed"
                document["message"] = "后端重启导致执行中断，可复用原邮箱重试"
            app.state.account_rebind_deleted_tasks.discard(task_id)
            app.state.account_rebind_tasks[task_id] = document
            _refresh_rebind_task(document)
            loaded += 1
        if loaded:
            _rebind_log(
                "INFO",
                f"已从 MongoDB 恢复 {loaded} 个换绑任务",
                step="task.recovered",
            )
        return loaded

    async def _repair_confirmed_cookie_conflicts() -> int:
        """Repair email changes confirmed remotely before cookie-copy failed."""
        repaired = 0
        for task in app.state.account_rebind_tasks.values():
            task_changed = False
            for item in task.get("items", []):
                if (
                    str(item.get("error") or "") != "internal_error:CookieConflict"
                    or str(item.get("failedStep") or "") != "rebind.verify"
                    or item.get("emailChanged")
                ):
                    continue
                account_id = str(item.get("accountId") or "")
                mailbox_id = str(item.get("mailboxId") or "")
                run_id = str(item.get("runId") or "")
                account = await resource_store.accounts.find_one({"_id": account_id})
                mailbox = await resource_store.emails.find_one(
                    {
                        "_id": mailbox_id,
                        "rebindReservedBy": run_id,
                        "status": {"$in": ["reserved", "available"]},
                        "usagePurpose": "rebind",
                    }
                )
                if not account or not mailbox:
                    continue
                old_email = str(account.get("email") or "").strip().lower()
                new_email = str(mailbox.get("email") or "").strip().lower()
                if not new_email:
                    continue
                await resource_store.mark_rebind_email_changed(
                    account_id,
                    old_email,
                    new_email,
                    str(mailbox.get("accessUrl") or ""),
                    str(item.get("proxy") or task.get("proxy") or ""),
                    str(item.get("proxyCountry") or task.get("proxyCountry") or ""),
                )
                await resource_store.consume_rebind_email(mailbox_id, run_id)
                item.update(
                    email=new_email,
                    emailChanged=True,
                    mailboxConsumed=True,
                    tokenRefreshOnly=True,
                    error="auth_token_refresh_required",
                    failedStep="confirm.login",
                    message="换绑确认已完成并补写账号池；请重试以获取新 AT",
                )
                task_changed = True
                repaired += 1
                _rebind_log(
                    "WARN",
                    "已修复 Cookie 冲突后的换绑状态：新邮箱已写入账号池，等待刷新 AT",
                    task_id=str(task.get("taskId") or ""),
                    account_id=account_id,
                    step="rebind.changed",
                    percent=78,
                )
            if task_changed:
                _refresh_rebind_task(task)
                await resource_store.save_rebind_task(task)
        return repaired

    async def _restore_rebind_mailbox_leases() -> int:
        """Restore rebind reservations released by older generic cleanup code."""
        restored = 0
        for task in app.state.account_rebind_tasks.values():
            for item in task.get("items", []):
                if item.get("status") == "success" or item.get("emailChanged"):
                    continue
                mailbox_id = str(item.get("mailboxId") or "")
                run_id = str(item.get("runId") or "")
                if not mailbox_id or not run_id:
                    continue
                mailbox = await resource_store.emails.find_one(
                    {
                        "_id": mailbox_id,
                        "status": "available",
                        "usagePurpose": "rebind",
                        "rebindReservedBy": run_id,
                    }
                )
                if mailbox and await resource_store.reserve_specific_rebind_email(
                    mailbox_id, run_id
                ):
                    restored += 1
        if restored:
            _rebind_log(
                "WARN",
                f"已恢复 {restored} 个被旧版清理逻辑误释放的换绑邮箱租约",
                step="mailbox.reserved",
            )
        return restored

    async def _check_rebind_promotion(
        task_id: str,
        item: dict,
        account_id: str,
        country: str,
    ) -> None:
        """Reuse the account plan API with a proxy from the rebind country."""
        normalized_country = str(country or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized_country):
            item.update(
                promotionCheckStatus="skipped",
                promotionCheckMessage="换绑代理国家未记录，已跳过自动优惠检测",
            )
            return
        item.update(
            progress=max(int(item.get("progress") or 0), 96),
            step="promotion.check",
            stepLabel=_rebind_step_label("promotion.check"),
            message=f"正在使用 {normalized_country} 代理自动检测优惠资格",
            promotionCheckCountry=normalized_country,
            promotionCheckStatus="running",
        )
        _refresh_rebind_task(app.state.account_rebind_tasks.get(task_id, {}))
        _rebind_log(
            "INFO",
            f"换绑完成，正在使用 {normalized_country} 代理自动检测优惠资格",
            task_id=task_id,
            account_id=account_id,
            step="promotion.check",
            percent=96,
        )
        try:
            result = await plan_check_service.check_accounts(
                [account_id], country=normalized_country
            )
            succeeded = result.succeeded == 1
            error_code = next(
                (
                    str(entry.errorCode or "")
                    for entry in result.items
                    if entry.status != "success"
                ),
                "",
            )
            item.update(
                promotionCheckStatus="success" if succeeded else "failed",
                promotionCheckError=error_code,
                promotionCheckMessage=(
                    f"已使用 {normalized_country} 代理完成优惠资格检测"
                    if succeeded
                    else f"{normalized_country} 优惠资格检测失败：{error_code or 'unknown'}"
                ),
            )
            _rebind_log(
                "INFO" if succeeded else "WARN",
                str(item["promotionCheckMessage"]),
                task_id=task_id,
                account_id=account_id,
                step="promotion.success" if succeeded else "promotion.failed",
                percent=98,
            )
        except Exception as exc:
            error_code = _safe_error(type(exc).__name__)
            item.update(
                promotionCheckStatus="failed",
                promotionCheckError=error_code,
                promotionCheckMessage=f"{normalized_country} 优惠资格检测异常：{error_code}",
            )
            _rebind_log(
                "WARN",
                str(item["promotionCheckMessage"]),
                task_id=task_id,
                account_id=account_id,
                step="promotion.failed",
                percent=98,
            )

    async def _run_rebind_item(
        task_id: str,
        task: dict,
        item: dict,
        proxy_selector: dict,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            if item.get("status") != "queued":
                return
            account_id = str(item.get("accountId") or "")
            run_id = str(item.get("runId") or "")
            mailbox_id = str(item.get("mailboxId") or "")
            proxy_owner = f"rebind:{task_id}:{account_id}"
            proxy_lease = None
            proxy = ""
            item.update(
                status="running",
                progress=6,
                error="",
                step="workflow.start",
                stepLabel=_rebind_step_label("workflow.start"),
                message="账号已进入执行队列，正在领取代理",
                startedAt=datetime.now(timezone.utc).isoformat(),
            )
            _refresh_rebind_task(task)
            _rebind_log(
                "INFO",
                f"账号 {item.get('email', '')} 开始执行换绑",
                task_id=task_id,
                account_id=account_id,
                step="workflow.start",
            )
            try:
                item_proxy_selector = proxy_selector
                preferred_country = str(
                    item.get("preferredProxyCountry") or ""
                ).strip().upper()
                if item.get("tokenRefreshOnly") and re.fullmatch(
                    r"[A-Z]{2}", preferred_country
                ):
                    item_proxy_selector = {
                        "mode": "pool",
                        "country": preferred_country,
                        "group": "",
                    }
                proxy_lease, proxy, proxy_attempt = await _acquire_verified_rebind_proxy(
                    task_id,
                    account_id,
                    item_proxy_selector,
                    proxy_owner,
                )
                if not proxy:
                    raise AccountRebindError("no_eligible_proxy", retryable=True)
                item.update(
                    proxy=_masked_proxy(proxy),
                    proxyId=str(getattr(proxy_lease, "id", "") or ""),
                    proxyCountry=str(getattr(proxy_lease, "country", "") or item_proxy_selector.get("country") or ""),
                    proxyGroup=str(getattr(proxy_lease, "group", "") or item_proxy_selector.get("group") or ""),
                    proxyAttempt=proxy_attempt,
                    progress=8,
                    step="proxy.acquired",
                    stepLabel=_rebind_step_label("proxy.acquired"),
                    message=f"实际执行代理已验证：{_masked_proxy(proxy)}",
                )
                _rebind_log(
                    "INFO",
                    f"实际执行代理已验证：{_masked_proxy(proxy)}；国家 {item['proxyCountry'] or '未标记'}；分组 {item['proxyGroup'] or '未分组'}",
                    task_id=task_id,
                    account_id=account_id,
                    step="proxy.acquired",
                    percent=8,
                )
                account = await resource_store.accounts.find_one({"_id": account_id})
                if not account:
                    raise AccountRebindError("account_not_found")
                account = dict(account)
                if not str(account.get("emailAccessUrl") or "").strip():
                    old_mailbox = await resource_store.emails.find_one(
                        {"emailNormalized": str(account.get("email") or "").strip().lower()}
                    )
                    if old_mailbox:
                        account["emailAccessUrl"] = str(old_mailbox.get("accessUrl") or "")
                account["emailAccessUrl"] = direct_mailbox_access_url(
                    str(account.get("emailAccessUrl") or ""), str(account.get("email") or "")
                )
                loop = asyncio.get_running_loop()

                def progress(step: str, percent: int, message: str) -> None:
                    def apply_progress() -> None:
                        item["progress"] = max(int(item.get("progress") or 0), percent)
                        item["message"] = message
                        item["step"] = step
                        item["stepLabel"] = _rebind_step_label(step)
                        history = item.setdefault("history", [])
                        history.append(
                            {
                                "time": datetime.now(timezone.utc).isoformat(),
                                "step": step,
                                "stepLabel": _rebind_step_label(step),
                                "percent": percent,
                                "message": message,
                            }
                        )
                        if len(history) > 30:
                            del history[:-30]
                        _refresh_rebind_task(task)
                        _rebind_log(
                            "INFO",
                            message,
                            task_id=task_id,
                            account_id=account_id,
                            step=step,
                            percent=percent,
                        )

                    loop.call_soon_threadsafe(apply_progress)

                if item.get("tokenRefreshOnly"):
                    item.update(
                        step="token.refresh",
                        stepLabel=_rebind_step_label("token.refresh"),
                        message="正在使用当前邮箱重新登录获取 AccessToken",
                        progress=12,
                    )
                    refreshed = await asyncio.to_thread(
                        AccountRebindService().refresh_access_token,
                        account,
                        proxy=proxy,
                        progress=progress,
                    )
                    await resource_store.update_account_access_token(
                        account_id,
                        refreshed.access_token,
                        refreshed.access_token_expires_at,
                        rebind_success=str(account.get("rebindStatus") or "")
                        == "email_changed_token_pending",
                    )
                    promotion_country = str(
                        account.get("rebindProxyCountry")
                        or item.get("preferredProxyCountry")
                        or item.get("proxyCountry")
                        or ""
                    ).upper()
                    await _check_rebind_promotion(
                        task_id, item, account_id, promotion_country
                    )
                    item.update(
                        status="success",
                        progress=100,
                        email=str(account.get("email") or ""),
                        loginBranch=refreshed.branch,
                        error="",
                        message="AccessToken 已重新获取并写入账号池",
                        step="token.refresh_success",
                        stepLabel=_rebind_step_label("token.refresh_success"),
                        completedAt=datetime.now(timezone.utc).isoformat(),
                    )
                    _rebind_log(
                        "INFO",
                        "AccessToken 已重新获取并写入账号池",
                        task_id=task_id,
                        account_id=account_id,
                        step="token.refresh_success",
                        percent=100,
                    )
                    return

                mailbox = await resource_store.emails.find_one(
                    {"_id": mailbox_id, "rebindReservedBy": run_id, "status": "reserved"}
                )
                if not mailbox:
                    raise AccountRebindError("reserved_mailbox_not_found")
                mailbox = dict(mailbox)
                stored_new_access_url = str(mailbox.get("accessUrl") or "")
                mailbox["accessUrl"] = direct_mailbox_access_url(
                    str(mailbox.get("accessUrl") or ""), str(mailbox.get("email") or "")
                )

                async def persist_email_changed(old_email: str, new_email: str) -> None:
                    await resource_store.mark_rebind_email_changed(
                        account_id,
                        old_email,
                        new_email,
                        stored_new_access_url,
                        _masked_proxy(proxy),
                        str(item.get("proxyCountry") or ""),
                    )
                    consumed = await resource_store.consume_rebind_email(mailbox_id, run_id)
                    if not consumed:
                        await resource_store.emails.update_one(
                            {"_id": mailbox_id},
                            {
                                "$set": {"status": "used", "usagePurpose": "rebind"},
                                "$unset": {"rebindReservedBy": "", "reservedAt": ""},
                            },
                        )
                    item.update(
                        emailChanged=True,
                        mailboxConsumed=True,
                        email=new_email,
                        progress=max(int(item.get("progress") or 0), 78),
                        step="rebind.changed",
                        stepLabel=_rebind_step_label("rebind.changed"),
                        message="远端换绑已完成，新邮箱已立即写入账号池；正在获取新 AT",
                    )
                    _rebind_log(
                        "INFO",
                        "远端换绑已完成，新邮箱和接码地址已立即写入账号池",
                        task_id=task_id,
                        account_id=account_id,
                        step="rebind.changed",
                        percent=78,
                    )

                def email_changed(old_email: str, new_email: str) -> None:
                    future = asyncio.run_coroutine_threadsafe(
                        persist_email_changed(old_email, new_email), loop
                    )
                    future.result(timeout=60)

                result = await asyncio.to_thread(
                    AccountRebindService().rebind,
                    account,
                    mailbox,
                    proxy=proxy,
                    progress=progress,
                    email_changed=email_changed,
                )
                await resource_store.mark_rebind_success(
                    account_id,
                    result.old_email,
                    result.new_email,
                    stored_new_access_url,
                    result.access_token,
                    result.access_token_expires_at,
                    _masked_proxy(proxy),
                    str(item.get("proxyCountry") or ""),
                )
                await _check_rebind_promotion(
                    task_id,
                    item,
                    account_id,
                    str(item.get("proxyCountry") or "").upper(),
                )
                consumed = bool(item.get("mailboxConsumed")) or await resource_store.consume_rebind_email(mailbox_id, run_id)
                if not consumed:
                    # Remote rebind and account update are already confirmed. Force this
                    # exact mailbox to used so it can never be allocated a second time.
                    await resource_store.emails.update_one(
                        {"_id": mailbox_id},
                        {
                            "$set": {"status": "used", "usagePurpose": "rebind"},
                            "$unset": {"rebindReservedBy": "", "reservedAt": ""},
                        },
                    )
                    _rebind_log(
                        "WARN",
                        "换绑成功后邮箱占用状态已执行一致性修复",
                        task_id=task_id,
                        account_id=account_id,
                        step="mailbox.consume_repaired",
                    )
                item.update(
                    status="success",
                    progress=100,
                    email=result.new_email,
                    loginBranch=result.login_branch,
                    confirmationBranch=result.confirmation_branch,
                    error="",
                    message="换绑成功，账号池和 AccessToken 已更新",
                    step="workflow.success",
                    stepLabel=_rebind_step_label("workflow.success"),
                    completedAt=datetime.now(timezone.utc).isoformat(),
                )
                _rebind_log(
                    "INFO",
                    f"账号已换绑为 {result.new_email}，新 AT 已写入账号池",
                    task_id=task_id,
                    account_id=account_id,
                    step="workflow.success",
                    percent=100,
                )
            except asyncio.CancelledError:
                # A running protocol call cannot be safely interrupted in a worker thread.
                # Keep its mailbox reserved so it cannot be reused ambiguously.
                item.update(status="cancelled", error="cancelled", message="任务已取消，邮箱仍保留待人工确认")
                raise
            except Exception as exc:
                code = (
                    exc.code
                    if isinstance(exc, AccountRebindError)
                    else f"internal_error:{type(exc).__name__}"
                )
                failed_step = str(item.get("step") or "")
                email_already_changed = bool(item.get("emailChanged"))
                token_refresh_only = bool(item.get("tokenRefreshOnly"))
                failure_message = (
                    f"邮箱已经换绑并写入账号池，但 {_rebind_error_message(code)}；请重新获取 AT"
                    if email_already_changed
                    else f"重新获取 AT 失败：{_rebind_error_message(code)}"
                    if token_refresh_only
                    else f"{_rebind_error_message(code)}；原分配邮箱继续保留供重试"
                )
                item.update(
                    status="failed",
                    error=code,
                    retryable=bool(
                        isinstance(exc, AccountRebindError) and exc.retryable
                    ),
                    message=failure_message,
                    failedStep=failed_step,
                    step="workflow.failed",
                    stepLabel=_rebind_step_label("workflow.failed"),
                    completedAt=datetime.now(timezone.utc).isoformat(),
                )
                account_updates = {
                    "rebindError": code,
                    "rebindProxy": _masked_proxy(proxy) if proxy else str(task.get("proxy") or ""),
                    "updatedAt": datetime.now(timezone.utc),
                }
                if not email_already_changed and not token_refresh_only:
                    account_updates["rebindStatus"] = "failed"
                await resource_store.accounts.update_one(
                    {"_id": account_id},
                    {"$set": account_updates},
                )
                if (
                    proxy_lease is not None
                    and str(proxy_selector.get("mode") or "") == "pool"
                    and isinstance(exc, AccountRebindError)
                    and exc.retryable
                    and not code.startswith("mailbox_network_error:")
                ):
                    await probe_store.record_proxy_registration_rejection(
                        proxy_lease.id,
                        code=code,
                        cooldown_seconds=600,
                    )
                _rebind_log(
                    "ERROR",
                    f"{failure_message}（错误码：{code}）",
                    task_id=task_id,
                    account_id=account_id,
                    step="workflow.failed",
                    percent=int(item.get("progress") or 0),
                )
            finally:
                if proxy_lease is not None:
                    await probe_store.release_proxy(proxy_lease.id, proxy_owner)
                _refresh_rebind_task(task)

    async def _run_rebind_task(
        task_id: str,
        task: dict,
        proxy_selector: dict,
        semaphore: asyncio.Semaphore,
    ) -> None:
        task["status"] = "running"
        task["message"] = "正在执行账号换绑"
        try:
            max_attempts = 1
            if str(proxy_selector.get("mode") or "") == "pool":
                max_attempts = min(
                    5,
                    max(
                        1,
                        await probe_store.count_eligible_proxies(
                            str(proxy_selector.get("country") or "") or None,
                            str(proxy_selector.get("group") or "") or None,
                        ),
                    ),
                )
            for attempt in range(1, max_attempts + 1):
                await asyncio.gather(
                    *(
                        _run_rebind_item(task_id, task, item, proxy_selector, semaphore)
                        for item in task.get("items", [])
                        if item.get("status") == "queued"
                    )
                )
                retry_items = [
                    item
                    for item in task.get("items", [])
                    if item.get("status") == "failed"
                    and item.get("retryable") is True
                    and not str(item.get("error") or "").startswith("mailbox_network_error:")
                    and str(item.get("failedStep") or "")
                    not in {"rebind.verify", "confirm.login", "complete"}
                ]
                if attempt >= max_attempts or not retry_items or task.get("cancelRequested"):
                    break
                for item in retry_items:
                    item.update(
                        status="queued",
                        progress=5,
                        step="proxy.retry",
                        stepLabel=_rebind_step_label("proxy.retry"),
                        message=f"连接失败，正在自动更换代理（第 {attempt + 1}/{max_attempts} 次）",
                        retryCount=int(item.get("retryCount") or 0) + 1,
                    )
                    _rebind_log(
                        "WARN",
                        item["message"],
                        task_id=task_id,
                        account_id=str(item.get("accountId") or ""),
                        step="proxy.retry",
                        percent=5,
                    )
                _refresh_rebind_task(task)
        finally:
            _refresh_rebind_task(task)
            if task.get("cancelRequested"):
                released = 0
                for item in task.get("items", []):
                    if item.get("status") == "success":
                        continue
                    if item.get("mailboxId") and await resource_store.release_rebind_email(
                        item["mailboxId"], item.get("runId", "")
                    ):
                        released += 1
                    await resource_store.clear_rebind_retry_state(
                        str(item.get("accountId") or "")
                    )
                    item.update(
                        status="cancelled",
                        step="workflow.cancelled",
                        stepLabel="任务已取消",
                        message="任务已结束，换绑邮箱已释放",
                    )
                app.state.account_rebind_deleted_tasks.add(task_id)
                app.state.account_rebind_tasks.pop(task_id, None)
                await resource_store.delete_rebind_task(task_id)
                _rebind_log(
                    "WARN",
                    f"取消已完成，释放 {released} 个换绑邮箱，任务已从列表移除",
                    task_id=task_id,
                    step="workflow.cancelled",
                )
            app.state.account_rebind_jobs.pop(task_id, None)

    @app.get("/api/account-rebind/pools")
    async def account_rebind_pools(request: Request) -> dict:
        mongo.require_online()
        available = await resource_store.emails.count_documents({"status": "available", "usagePurpose": {"$ne": "rebind"}})
        available_standard = await resource_store.emails.count_documents(
            {
                "status": "available",
                "usagePurpose": {"$ne": "rebind"},
                "$or": [
                    {"sourceType": {"$exists": False}},
                    {"sourceType": {"$ne": "mailcom_alias"}},
                ],
            }
        )
        available_aliases = await resource_store.emails.count_documents(
            {
                "status": "available",
                "usagePurpose": {"$ne": "rebind"},
                "sourceType": "mailcom_alias",
            }
        )
        reserved = await resource_store.emails.count_documents({"status": "reserved", "usagePurpose": "rebind"})
        expired_access_tokens = await resource_store.accounts.count_documents(
            {
                "$and": [
                    {
                        "$or": [
                            {"accessTokenExpiresAt": {"$lte": datetime.now(timezone.utc)}},
                            {"aliveStatus": "dead", "accessTokenConfigured": True},
                            {"rebindStatus": "email_changed_token_pending"},
                        ]
                    },
                    {
                        "$or": [
                            {"chatgptPassword": {"$type": "string", "$ne": ""}},
                            {"emailAccessUrl": {"$type": "string", "$ne": ""}},
                        ]
                    },
                ]
            }
        )
        success = await resource_store.accounts.find(
            {"rebindStatus": {"$in": ["success", "email_changed_token_pending"]}},
            {
                "email": 1,
                "previousEmail": 1,
                "reboundEmail": 1,
                "accessTokenConfigured": 1,
                "rebindProxy": 1,
                "rebindStatus": 1,
            },
        ).sort("updatedAt", -1).to_list(length=100)
        return {
            "availableRegistrationEmails": await resource_store.emails.count_documents({"status": "available", "$or": [{"usagePurpose": {"$exists": False}}, {"usagePurpose": "registration"}]}),
            "availableRebindEmails": available,
            "availableStandardEmails": available_standard,
            "availableAliasEmails": available_aliases,
            "reservedRebindEmails": reserved,
            "expiredAccessTokens": expired_access_tokens,
            "concurrency": int(request.app.state.account_rebind_concurrency),
            "proxy": request.app.state.account_rebind_proxy,
            "success": [{"id": str(item.get("_id")), **{k: item.get(k) for k in ("email", "previousEmail", "reboundEmail", "accessTokenConfigured", "rebindProxy", "rebindStatus")}} for item in success],
        }

    @app.put("/api/account-rebind/proxy")
    async def set_account_rebind_proxy(request: Request) -> dict:
        payload = await request.json()
        proxy = str(payload.get("proxy") or "").strip()
        if proxy and not urlparse(proxy).scheme:
            raise HTTPException(status_code=422, detail="代理地址必须包含协议，例如 http://")
        request.app.state.account_rebind_proxy = proxy
        return {"ok": True, "proxy": proxy}

    async def _recover_failed_rebind_tasks() -> int:
        """Rebuild retryable UI tasks from durable account/mailbox state."""
        existing_accounts = {
            str(item.get("accountId") or "")
            for task in app.state.account_rebind_tasks.values()
            for item in task.get("items", [])
        }
        failed_accounts = await resource_store.accounts.find(
            {"rebindStatus": {"$in": ["failed", "in_progress", "email_changed_token_pending"]}},
            {
                "_id": 1,
                "email": 1,
                "rebindError": 1,
                "rebindStatus": 1,
                "rebindMailboxId": 1,
                "rebindRunId": 1,
                "rebindMailboxSource": 1,
                "updatedAt": 1,
            },
        ).to_list(length=500)
        if not failed_accounts:
            return 0
        reserved_mailboxes = await resource_store.emails.find(
            {
                "status": "reserved",
                "usagePurpose": "rebind",
                "rebindReservedBy": {"$type": "string", "$ne": ""},
            },
            {
                "_id": 1,
                "email": 1,
                "sourceType": 1,
                "rebindReservedBy": 1,
                "reservedAt": 1,
            },
        ).to_list(length=500)
        recovered = 0
        for account in failed_accounts:
            account_id = str(account.get("_id") or "")
            if not account_id or account_id in existing_accounts:
                continue
            suffix = f"-{account_id}"
            mailbox = next(
                (
                    item
                    for item in reserved_mailboxes
                    if str(item.get("rebindReservedBy") or "").startswith("rebind-")
                    and str(item.get("rebindReservedBy") or "").endswith(suffix)
                ),
                None,
            )
            if mailbox is not None:
                run_id = str(mailbox.get("rebindReservedBy") or "")
                task_id = run_id[len("rebind-") : -len(suffix)] or "recovered-failed"
            else:
                task_id = "recovered-failed"
                run_id = str(account.get("rebindRunId") or f"rebind-{task_id}-{account_id}")
            source = str(
                (mailbox or {}).get("sourceType")
                or account.get("rebindMailboxSource")
                or "standard"
            )
            source_label = "分裂邮箱" if source == "mailcom_alias" else "普通邮箱"
            item = {
                "accountId": account_id,
                "email": str(account.get("email") or ""),
                "status": "failed",
                "progress": 0,
                "step": "workflow.failed",
                "stepLabel": _rebind_step_label("workflow.failed"),
                "message": (
                    "从 MongoDB 恢复的失败任务，可复用原邮箱重试"
                    if mailbox is not None
                    else "从 MongoDB 恢复的失败账号，重试时将重新分配邮箱"
                ),
                "mailbox": str((mailbox or {}).get("email") or ""),
                "mailboxId": str(
                    (mailbox or {}).get("_id")
                    or account.get("rebindMailboxId")
                    or ""
                ),
                "mailboxSource": source,
                "mailboxSourceLabel": source_label,
                "error": str(account.get("rebindError") or "recovered_failed_task"),
                "runId": run_id,
                "tokenRefreshOnly": str(account.get("rebindStatus") or "")
                == "email_changed_token_pending",
            }
            task = app.state.account_rebind_tasks.get(task_id)
            if task is None:
                task = {
                    "taskId": task_id,
                    "status": "failed",
                    "progress": 0,
                    "proxy": "等待重新选择项目代理池",
                    "proxyId": "",
                    "emailSource": source,
                    "emailSourceLabel": source_label,
                    "createdAt": (
                        (mailbox or {}).get("reservedAt").isoformat()
                        if isinstance((mailbox or {}).get("reservedAt"), datetime)
                        else account.get("updatedAt").isoformat()
                        if isinstance(account.get("updatedAt"), datetime)
                        else datetime.now(timezone.utc).isoformat()
                    ),
                    "items": [],
                    "message": "从 MongoDB 恢复的失败任务",
                    "recovered": True,
                }
                app.state.account_rebind_tasks[task_id] = task
            task["items"].append(item)
            existing_accounts.add(account_id)
            recovered += 1
        if recovered:
            _rebind_log(
                "INFO",
                f"已从 MongoDB 恢复 {recovered} 个失败账号及其预留邮箱",
                step="task.recovered",
            )
        return recovered

    async def _remove_completed_accounts_from_failed_tasks() -> int:
        """Drop stale failed items when the account was completed by a later retry."""
        candidates = [
            str(item.get("accountId") or "")
            for task in app.state.account_rebind_tasks.values()
            for item in task.get("items", [])
            if item.get("status") == "failed" and item.get("accountId")
        ]
        if not candidates:
            return 0
        completed = {
            str(document.get("_id") or "")
            for document in await resource_store.accounts.find(
                {
                    "_id": {"$in": list(dict.fromkeys(candidates))},
                    "rebindStatus": "success",
                },
                {"_id": 1},
            ).to_list(length=len(set(candidates)))
        }
        if not completed:
            return 0
        removed = 0
        for task_id, task in list(app.state.account_rebind_tasks.items()):
            original_items = list(task.get("items", []))
            remaining_items = [
                item
                for item in original_items
                if not (
                    item.get("status") == "failed"
                    and str(item.get("accountId") or "") in completed
                )
            ]
            removed_here = len(original_items) - len(remaining_items)
            if not removed_here:
                continue
            removed += removed_here
            if not remaining_items:
                app.state.account_rebind_tasks.pop(task_id, None)
                app.state.account_rebind_deleted_tasks.add(task_id)
                await resource_store.delete_rebind_task(task_id)
                continue
            task["items"] = remaining_items
            _refresh_rebind_task(task)
            await resource_store.save_rebind_task(task)
        if removed:
            _rebind_log(
                "INFO",
                f"已从失败队列清理 {removed} 个后来换绑成功的账号",
                step="task.reconciled",
            )
        return removed

    @app.get("/api/account-rebind/tasks")
    async def account_rebind_tasks(request: Request) -> dict:
        mongo.require_online()
        await _load_durable_rebind_tasks()
        await _repair_confirmed_cookie_conflicts()
        await _restore_rebind_mailbox_leases()
        await _recover_failed_rebind_tasks()
        await _remove_completed_accounts_from_failed_tasks()
        return {"items": list(request.app.state.account_rebind_tasks.values())}

    @app.get("/api/account-rebind/logs")
    async def account_rebind_logs(request: Request) -> dict:
        mongo.require_online()
        durable = await resource_store.list_rebind_logs(300)
        return {"items": durable or request.app.state.account_rebind_logs[-300:]}

    @app.put("/api/account-rebind/concurrency")
    async def set_account_rebind_concurrency(request: Request) -> dict:
        payload = await request.json()
        value = max(1, min(20, int(payload.get("concurrency") or 1)))
        active_jobs = [
            job
            for job in request.app.state.account_rebind_jobs.values()
            if not job.done()
        ]
        if active_jobs and value != request.app.state.account_rebind_concurrency:
            raise HTTPException(
                status_code=409,
                detail="已有换绑账号正在执行，请等待完成后再修改全局并发数",
            )
        request.app.state.account_rebind_concurrency = value
        request.app.state.account_rebind_semaphore = asyncio.Semaphore(value)
        _rebind_log("INFO", f"全局换绑并发已设置为 {value}", step="concurrency.updated")
        return {"concurrency": value}

    @app.get("/api/account-rebind/proxies")
    async def account_rebind_proxies() -> dict:
        mongo.require_online()
        countries = await resource_store.proxy_country_summaries()
        groups = await resource_store.proxy_group_summaries()
        docs = await resource_store.proxies.find({"enabled": {"$ne": False}, "status": {"$in": ["available", "unknown"]}}, {"_id": 1, "country": 1, "group": 1, "scheme": 1, "host": 1, "port": 1}).sort("country", 1).to_list(length=500)
        items = [{"id": "local7890", "label": "本地代理 · 127.0.0.1:7890", "value": "http://127.0.0.1:7890", "source": "local"}]
        items.extend({"id": str(doc["_id"]), "label": f"{doc.get('country', 'ZZ')} · {doc.get('group', 'default')} · {doc.get('host')}:{doc.get('port')}", "value": "", "source": "pool"} for doc in docs)
        country_payload = []
        for item in countries:
            data = item.model_dump()
            data["rebindAvailable"] = await probe_store.count_eligible_proxies(item.country)
            country_payload.append(data)
        group_payload = []
        for item in groups:
            data = item.model_dump()
            data["rebindAvailable"] = await probe_store.count_eligible_proxies(
                item.country, item.group
            )
            group_payload.append(data)
        return {
            "items": items,
            "countries": country_payload,
            "groups": group_payload,
        }

    @app.post("/api/account-rebind/tasks")
    async def create_account_rebind_task(request: Request) -> dict:
        mongo.require_online()
        payload = await request.json()
        account_ids = [str(item) for item in (payload.get("accountIds") or []) if str(item).strip()]
        if not account_ids:
            raise HTTPException(status_code=422, detail="请先选择账号")
        proxy = str(payload.get("proxy") or "").strip()
        task_id = uuid4().hex
        accounts = await resource_store.accounts.find({"_id": {"$in": account_ids}}, {"_id": 1, "email": 1}).to_list(length=len(account_ids))
        items = []
        for account in accounts:
            items.append({"accountId": str(account["_id"]), "email": account.get("email", ""), "status": "pending", "progress": 0, "step": "task.created", "stepLabel": _rebind_step_label("task.created"), "message": "等待选择代理和邮箱类型", "mailbox": "", "mailboxId": "", "error": "", "runId": f"rebind-{task_id}-{account['_id']}"})
        task = {"taskId": task_id, "status": "pending", "progress": 0, "proxy": proxy, "proxyId": "", "emailSource": "standard", "createdAt": datetime.now(timezone.utc).isoformat(), "items": items, "message": "等待选择代理、邮箱类型并开始换绑"}
        request.app.state.account_rebind_tasks[task_id] = task
        request.app.state.account_rebind_deleted_tasks.discard(task_id)
        await resource_store.save_rebind_task(task)
        _rebind_log("INFO", f"任务 {task_id[:8]} 已创建，共 {len(items)} 个账号", task_id=task_id, step="task.created", percent=0)
        return task

    @app.post("/api/account-rebind/tasks/{task_id}/start")
    async def start_account_rebind_task(task_id: str, request: Request) -> dict:
        await _load_durable_rebind_tasks()
        task = request.app.state.account_rebind_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="换绑任务不存在")
        existing_job = request.app.state.account_rebind_jobs.get(task_id)
        if existing_job and not existing_job.done():
            raise HTTPException(status_code=409, detail="换绑任务正在运行")
        if task.get("status") in {"success", "running", "queued"}:
            raise HTTPException(status_code=409, detail="换绑任务已启动或已完成")
        payload = await request.json()
        mode = str(payload.get("proxyMode") or "").strip().lower()
        retry_failed_only = bool(
            payload.get("retryFailedOnly")
            or getattr(request.state, "account_rebind_retry_failed_only", False)
        )
        if mode not in {"pool", "auto", "local", "custom"}:
            raise HTTPException(status_code=422, detail="请选择代理来源，系统不会默认使用本地代理")
        proxy_id = str(payload.get("proxyId") or "").strip()
        custom_proxy = str(payload.get("proxy") or "").strip()
        country = str(payload.get("country") or "").strip().upper()
        group = " ".join(str(payload.get("group") or "").split())
        email_source = str(payload.get("emailSource") or "standard").strip().casefold()
        if email_source not in {"standard", "mailcom_alias"}:
            raise HTTPException(status_code=422, detail="邮箱类型必须是普通邮箱或分裂邮箱")
        email_source_label = "分裂邮箱" if email_source == "mailcom_alias" else "普通邮箱"
        if mode == "auto":
            selector = {"mode": "pool", "country": "", "group": ""}
            proxy_label = "项目代理池 · 自动轮换全部可用代理"
        elif mode == "pool" or (country and not mode):
            if not re.fullmatch(r"[A-Z]{2}", country):
                raise HTTPException(status_code=422, detail="请先选择代理国家")
            if not group:
                raise HTTPException(status_code=422, detail="请先选择代理分组")
            selector = {"mode": "pool", "country": country, "group": group}
            proxy_label = f"代理池 · {country} · {group or '全部分组'}"
        elif mode == "custom" or (custom_proxy and not proxy_id):
            if not custom_proxy or not urlparse(custom_proxy).scheme:
                raise HTTPException(status_code=422, detail="自定义代理必须包含协议")
            selector = {"mode": "custom", "proxy": custom_proxy}
            proxy_label = _masked_proxy(custom_proxy)
        else:
            selected_id = proxy_id or "local7890"
            selector = {"mode": "selected", "proxyId": selected_id, "country": country}
            proxy_label = "本地代理 · 127.0.0.1:7890" if selected_id == "local7890" else "代理池指定节点"
        if task.get("tokenRefreshTask"):
            preferred_countries = sorted({
                str(item.get("preferredProxyCountry") or "").strip().upper()
                for item in task.get("items", [])
                if re.fullmatch(
                    r"[A-Z]{2}",
                    str(item.get("preferredProxyCountry") or "").strip().upper(),
                )
            })
            proxy_label = (
                f"按原换绑国家获取代理 · {preferred_countries[0]}"
                if len(preferred_countries) == 1
                else "按每个账号原换绑国家分别获取代理"
                if preferred_countries
                else proxy_label
            )
            checked_proxy = "由每个账号在执行时按国家独立验证"
        else:
            _rebind_log(
                "INFO",
                f"正在预检 {proxy_label}",
                task_id=task_id,
                step="proxy.preflight",
            )
            try:
                checked_proxy = await _preflight_rebind_proxy(task_id, selector)
            except AccountRebindError as exc:
                task["status"] = "pending"
                task["message"] = (
                    "当前代理出口触发 ChatGPT Cloudflare 挑战，请切换代理后重试"
                    if exc.code == "cloudflare_challenge_required"
                    else f"代理预检失败：{exc.code}"
                )
                _rebind_log(
                    "ERROR",
                    task["message"],
                    task_id=task_id,
                    step="proxy.preflight_failed",
                )
                raise HTTPException(
                    status_code=422,
                    detail={"code": exc.code, "message": task["message"], "retryable": True},
                ) from exc
        task.update(
            proxy=proxy_label,
            proxyId=proxy_id,
            proxyCountry=country,
            proxyGroup=group,
            proxySelector=selector,
            emailSource=email_source,
            emailSourceLabel=email_source_label,
            status="queued",
            message=f"正在从项目邮箱池分配{email_source_label}",
        )
        _rebind_log("INFO", f"任务 {task_id[:8]} 已选择 {proxy_label}，预检出口 {checked_proxy}", task_id=task_id, step="proxy.selected")
        for item in task.get("items", []):
            eligible_statuses = {"failed"} if retry_failed_only else {"pending", "failed", "released", "cancelled"}
            if item.get("status") not in eligible_statuses:
                continue
            account_state = await resource_store.accounts.find_one(
                {"_id": str(item.get("accountId") or "")},
                {
                    "rebindStatus": 1,
                    "rebindMailboxId": 1,
                    "rebindRunId": 1,
                    "rebindMailboxSource": 1,
                },
            )
            if item.get("tokenRefreshOnly") or str((account_state or {}).get("rebindStatus") or "") == "email_changed_token_pending":
                item.update(
                    tokenRefreshOnly=True,
                    status="queued",
                    progress=5,
                    step="token.refresh",
                    stepLabel=_rebind_step_label("token.refresh"),
                    message="邮箱已经换绑，复用当前账号资料重新获取 AT",
                    error="",
                    retryCount=int(item.get("retryCount") or 0) + (1 if retry_failed_only else 0),
                )
                _rebind_log(
                    "INFO",
                    item["message"],
                    task_id=task_id,
                    account_id=item["accountId"],
                    step="token.refresh",
                    percent=5,
                )
                continue
            item.update(step="mailbox.reserve", stepLabel=_rebind_step_label("mailbox.reserve"), message=f"正在从项目邮箱池申请{email_source_label}")
            _rebind_log("INFO", f"正在从项目邮箱池申请{email_source_label}", task_id=task_id, account_id=item["accountId"], step="mailbox.reserve", percent=2)
            mailbox = None
            reused_mailbox = False
            preferred_mailbox_id = str(
                item.get("mailboxId")
                or (account_state or {}).get("rebindMailboxId")
                or ""
            )
            if preferred_mailbox_id and item.get("runId"):
                mailbox = await resource_store.emails.find_one(
                    {
                        "_id": preferred_mailbox_id,
                        "rebindReservedBy": str(item["runId"]),
                        "status": "reserved",
                        "usagePurpose": "rebind",
                    }
                )
                reused_mailbox = mailbox is not None
                if mailbox is None:
                    mailbox = await resource_store.reserve_specific_rebind_email(
                        preferred_mailbox_id, str(item["runId"])
                    )
                    reused_mailbox = mailbox is not None
            if mailbox is None:
                mailbox = await resource_store.reserve_rebind_email(
                    item["runId"], item.get("email", ""), source=email_source
                )
            if mailbox is not None:
                await resource_store.remember_rebind_mailbox(
                    str(item["accountId"]), mailbox, str(item["runId"])
                )
            actual_source = str((mailbox or {}).get("sourceType") or email_source)
            actual_source_label = "分裂邮箱" if actual_source == "mailcom_alias" else "普通邮箱"
            mailbox_message = (
                f"复用已预留{actual_source_label}：{mailbox.get('email')}"
                if mailbox and reused_mailbox
                else f"已分配{actual_source_label}：{mailbox.get('email')}"
                if mailbox
                else f"项目邮箱池没有可用的{email_source_label}"
            )
            item.update({"status": "queued" if mailbox else "failed", "progress": 5 if mailbox else 0, "step": "mailbox.reserved" if mailbox else "mailbox.failed", "stepLabel": _rebind_step_label("mailbox.reserved" if mailbox else "mailbox.failed"), "message": mailbox_message, "mailbox": mailbox.get("email") if mailbox else item.get("mailbox", ""), "mailboxId": str(mailbox.get("_id")) if mailbox else item.get("mailboxId", ""), "mailboxSource": actual_source, "mailboxSourceLabel": actual_source_label, "error": "" if mailbox else "rebind_mailbox_empty", "retryable": False, "retryCount": int(item.get("retryCount") or 0) + (1 if retry_failed_only else 0)})
            _rebind_log("INFO" if mailbox else "ERROR", item["message"], task_id=task_id, account_id=item["accountId"], step="mailbox.reserved" if mailbox else "mailbox.failed", percent=5 if mailbox else 0)
        task["progress"] = min((item.get("progress", 0) for item in task["items"]), default=0)
        if not any(item.get("status") == "queued" for item in task.get("items", [])):
            _refresh_rebind_task(task)
            return task
        worker_semaphore = request.app.state.account_rebind_semaphore
        job = asyncio.create_task(
            _run_rebind_task(task_id, task, selector, worker_semaphore),
            name=f"account-rebind-{task_id}",
        )
        request.app.state.account_rebind_jobs[task_id] = job
        task["status"], task["message"] = "running", "换绑执行器已启动"
        await resource_store.save_rebind_task(task)
        _rebind_log("INFO", "换绑执行器已启动", task_id=task_id, step="workflow.dispatched")
        return task

    @app.post("/api/account-rebind/tasks/start")
    async def start_pending_account_rebind_tasks(request: Request) -> dict:
        await _load_durable_rebind_tasks()
        pending = [
            (task_id, task)
            for task_id, task in request.app.state.account_rebind_tasks.items()
            if any(
                item.get("status") == "pending"
                for item in task.get("items", [])
            )
            and not (
                request.app.state.account_rebind_jobs.get(task_id)
                and not request.app.state.account_rebind_jobs[task_id].done()
            )
        ]
        concurrency = request.app.state.account_rebind_concurrency
        request.app.state.account_rebind_semaphore = asyncio.Semaphore(concurrency)
        semaphore = asyncio.Semaphore(concurrency)
        async def run_one(task_id: str) -> dict:
            async with semaphore:
                return await start_account_rebind_task(task_id, request)
        results = await asyncio.gather(*(run_one(task_id) for task_id, _ in pending), return_exceptions=True)
        started = sum(not isinstance(result, Exception) for result in results)
        failures = [
            _safe_error(getattr(result, "detail", str(result)))
            for result in results
            if isinstance(result, Exception)
        ]
        request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO", "step": "batch.start", "message": f"批量开始 {started}/{len(pending)} 个任务，并发 {concurrency}"})
        return {
            "requested": len(pending),
            "started": started,
            "failed": len(failures),
            "errors": failures[:10],
            "concurrency": concurrency,
        }

    @app.post("/api/account-rebind/tasks/retry-failed")
    async def retry_failed_account_rebind_tasks(request: Request) -> dict:
        mongo.require_online()
        await _load_durable_rebind_tasks()
        await _repair_confirmed_cookie_conflicts()
        await _restore_rebind_mailbox_leases()
        await _recover_failed_rebind_tasks()
        failed = [
            (task_id, task)
            for task_id, task in request.app.state.account_rebind_tasks.items()
            if any(item.get("status") == "failed" for item in task.get("items", []))
            and not (
                request.app.state.account_rebind_jobs.get(task_id)
                and not request.app.state.account_rebind_jobs[task_id].done()
            )
        ]
        if not failed:
            return {"requested": 0, "started": 0, "failed": 0}
        request.state.account_rebind_retry_failed_only = True
        results = []
        for task_id, _task in failed:
            try:
                results.append(await start_account_rebind_task(task_id, request))
            except Exception as exc:
                results.append(exc)
        started = sum(not isinstance(result, Exception) for result in results)
        failures = [
            _safe_error(getattr(result, "detail", str(result)))
            for result in results
            if isinstance(result, Exception)
        ]
        _rebind_log(
            "INFO" if started else "ERROR",
            f"一键重试失败任务：已启动 {started}/{len(failed)} 个",
            step="batch.retry_failed",
        )
        return {
            "requested": len(failed),
            "started": started,
            "failed": len(failures),
            "errors": failures[:10],
        }

    @app.post("/api/account-rebind/access-tokens/refresh-expired")
    async def refresh_expired_account_access_tokens(request: Request) -> dict:
        """Queue current-email logins for the accounts selected in the pool UI."""
        mongo.require_online()
        await _load_durable_rebind_tasks()
        payload = await request.json()
        account_ids = list(dict.fromkeys(
            str(value).strip()
            for value in payload.get("accountIds", [])
            if str(value).strip()
        ))[:500]
        if not account_ids:
            raise HTTPException(status_code=422, detail="请先在账号池勾选要重新获取 AT 的账号")
        now = datetime.now(timezone.utc)
        accounts = await resource_store.accounts.find(
            {
                "$and": [
                    {"_id": {"$in": account_ids}},
                    {
                        "$or": [
                            {"chatgptPassword": {"$type": "string", "$ne": ""}},
                            {"emailAccessUrl": {"$type": "string", "$ne": ""}},
                        ]
                    },
                ]
            },
            {"_id": 1, "email": 1, "rebindProxyCountry": 1},
        ).to_list(length=len(account_ids))
        active_accounts = {
            str(item.get("accountId") or "")
            for task in app.state.account_rebind_tasks.values()
            for item in task.get("items", [])
            if item.get("status") in {"queued", "running"}
        }
        accounts = [
            account
            for account in accounts
            if str(account.get("_id") or "") not in active_accounts
        ]
        if not accounts:
            return {"requested": 0, "started": 0}

        task_id = f"token-{uuid4().hex}"
        items = [
            {
                "accountId": str(account["_id"]),
                "email": str(account.get("email") or ""),
                "status": "pending",
                "progress": 0,
                "step": "token.refresh",
                "stepLabel": _rebind_step_label("token.refresh"),
                "message": "等待重新登录获取过期 AccessToken",
                "mailbox": "",
                "mailboxId": "",
                "error": "",
                "tokenRefreshOnly": True,
                "preferredProxyCountry": str(account.get("rebindProxyCountry") or "").upper(),
                "runId": f"rebind-{task_id}-{account['_id']}",
            }
            for account in accounts
        ]
        task = {
            "taskId": task_id,
            "status": "pending",
            "progress": 0,
            "proxy": "等待选择代理池",
            "proxyId": "",
            "emailSource": "standard",
            "emailSourceLabel": "不分配新邮箱",
            "createdAt": now.isoformat(),
            "items": items,
            "message": "等待重新获取过期 AccessToken",
            "tokenRefreshTask": True,
        }
        app.state.account_rebind_tasks[task_id] = task
        app.state.account_rebind_deleted_tasks.discard(task_id)
        await resource_store.save_rebind_task(task)
        _rebind_log(
            "INFO",
            f"已为账号池选中的 {len(items)} 个账号创建 AT 刷新项",
            task_id=task_id,
            step="token.refresh",
            percent=0,
        )
        await start_account_rebind_task(task_id, request)
        return {"requested": len(items), "started": len(items), "taskId": task_id}

    @app.post("/api/account-rebind/tasks/{task_id}/release")
    async def release_account_rebind_task(task_id: str, request: Request) -> dict:
        task = request.app.state.account_rebind_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="换绑任务不存在")
        running = any(item.get("status") == "running" for item in task.get("items", []))
        released = 0
        for item in task.get("items", []):
            if item.get("status") == "running" or item.get("status") == "success":
                continue
            if item.get("mailboxId") and await resource_store.release_rebind_email(
                item["mailboxId"], item.get("runId", "")
            ):
                released += 1
            await resource_store.clear_rebind_retry_state(
                str(item.get("accountId") or "")
            )
            item["status"] = "cancelled"
        if running:
            task["cancelRequested"] = True
            task["message"] = "已请求取消；当前账号安全结束后会释放邮箱并移除任务"
            _rebind_log("WARN", task["message"], task_id=task_id, step="workflow.cancel_requested")
            return {"ok": True, "removed": False, "stopping": True, "released": released}
        request.app.state.account_rebind_deleted_tasks.add(task_id)
        request.app.state.account_rebind_tasks.pop(task_id, None)
        request.app.state.account_rebind_jobs.pop(task_id, None)
        await resource_store.delete_rebind_task(task_id)
        _rebind_log(
            "WARN",
            f"任务已取消，释放 {released} 个换绑邮箱，并从列表移除",
            task_id=task_id,
            step="workflow.cancelled",
        )
        return {"ok": True, "removed": True, "stopping": False, "released": released}

    @app.post("/api/account-rebind/tasks/cancel-all")
    async def cancel_all_account_rebind_tasks(request: Request) -> dict:
        cancelled = removed = stopping = released = 0
        for task_id, task in list(request.app.state.account_rebind_tasks.items()):
            running = any(item.get("status") == "running" for item in task.get("items", []))
            for item in task.get("items", []):
                if item.get("status") in {"running", "success"}:
                    continue
                if item.get("mailboxId") and await resource_store.release_rebind_email(
                    item["mailboxId"], item.get("runId", "")
                ):
                    released += 1
                await resource_store.clear_rebind_retry_state(
                    str(item.get("accountId") or "")
                )
                item["status"] = "cancelled"
            if running:
                task["cancelRequested"] = True
                task["message"] = "已请求取消；当前账号结束后自动移除"
                stopping += 1
            else:
                request.app.state.account_rebind_deleted_tasks.add(task_id)
                request.app.state.account_rebind_tasks.pop(task_id, None)
                request.app.state.account_rebind_jobs.pop(task_id, None)
                await resource_store.delete_rebind_task(task_id)
                removed += 1
            cancelled += 1
        _rebind_log(
            "WARN",
            f"取消 {cancelled} 个任务：已移除 {removed} 个，等待当前账号结束 {stopping} 个，释放邮箱 {released} 个",
            step="batch.cancel",
        )
        return {"cancelled": cancelled, "removed": removed, "stopping": stopping, "released": released}

    @app.post("/api/accounts", response_model=AccountRecord, status_code=201)
    async def create_account(payload: AccountCreate) -> AccountRecord:
        mongo.require_online()
        try:
            return await resource_service.create_account(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/accounts/import", response_model=ImportResult)
    async def import_accounts(payload: RawImportInput) -> ImportResult:
        mongo.require_online()
        return await resource_service.import_accounts(payload.rawText)

    @app.post("/api/accounts/bulk-delete", response_model=DeleteResult)
    async def delete_accounts(payload: BulkIdsInput) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_accounts(payload.ids)

    @app.post(
        "/api/accounts/check-promotion",
        response_model=AccountPlanCheckResult,
    )
    async def check_account_promotions(
        request: Request,
        payload: AccountPlanCheckInput,
    ) -> AccountPlanCheckResult:
        mongo.require_online()
        return await request.app.state.plan_check_service.check_accounts(
            payload.ids, proxy_id=payload.proxyId
        )

    @app.post("/api/accounts/check-global-promotion")
    async def check_account_global_promotions(payload: AccountPlanCheckInput) -> dict[str, int]:
        mongo.require_online()
        return await resource_store.queue_global_promotion_checks(payload.ids)

    @app.post(
        "/api/accounts/check-checkout-type",
        response_model=AccountCheckoutTypeCheckResult,
    )
    async def check_account_checkout_types(
        request: Request,
        payload: AccountCheckoutTypeCheckInput,
    ) -> AccountCheckoutTypeCheckResult:
        mongo.require_online()
        return await request.app.state.plan_check_service.check_checkout_types(
            payload.ids, proxy_id=payload.proxyId
        )

    @app.post("/api/accounts/check-oaics-all-proxies")
    async def check_account_oaics_all_proxies(
        request: Request, payload: AccountCheckoutTypeCheckInput
    ) -> dict[str, Any]:
        mongo.require_online()
        return await request.app.state.plan_check_service.start_oaics_scans(payload.ids)

    @app.post(
        "/api/accounts/check-alive",
        response_model=AccountAliveCheckResult,
    )
    async def check_accounts_alive(
        request: Request,
        payload: AccountAliveCheckInput,
    ) -> AccountAliveCheckResult:
        mongo.require_online()
        return await request.app.state.alive_check_service.check_accounts(
            payload.ids, proxy_id=payload.proxyId
        )

    @app.post("/api/accounts/export", response_model=TextExport)
    async def export_accounts(payload: AccountExportInput) -> TextExport:
        mongo.require_online()
        if payload.scope != "all" and not payload.ids:
            raise HTTPException(status_code=422, detail="选中导出必须提供账号 ID")
        return await resource_service.export_accounts(payload)

    @app.get("/api/emails", response_model=Page[EmailRecord])
    async def list_emails(
        page: PageNumber = 1,
        page_size: PageSizeOption = Query(PageSizeOption.TEN, alias="pageSize"),
        q: SearchQuery = "",
        source: Annotated[str, Query(pattern="^(all|standard|mailcom_alias)$")] = "all",
    ) -> Page[EmailRecord]:
        mongo.require_online()
        return await resource_store.list_emails(page, int(page_size), q, source)  # type: ignore[arg-type]

    @app.post("/api/emails/import", response_model=ImportResult)
    async def import_emails(payload: RawImportInput) -> ImportResult:
        mongo.require_online()
        return await resource_service.import_emails(payload.rawText)

    @app.post("/api/emails/sync-mailcom-aliases", response_model=ImportResult)
    async def sync_mailcom_aliases() -> ImportResult:
        mongo.require_online()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.get(
                    "http://127.0.0.1:3211/api/export/registration-items"
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "mailcom_hub_unavailable",
                    "message": "MailCom Hub 暂时不可用，请先启动本机邮箱管理器",
                },
            ) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "mailcom_hub_invalid_response",
                    "message": "MailCom Hub 返回格式无效",
                },
            )
        return await resource_service.sync_mailcom_aliases(items)

    @app.post("/api/emails/bulk-delete", response_model=DeleteResult)
    async def delete_emails(payload: BulkIdsInput) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_emails(payload.ids)

    @app.post("/api/emails/export", response_model=TextExport)
    async def export_emails(payload: EmailExportInput) -> TextExport:
        mongo.require_online()
        if payload.scope != "all" and not payload.ids:
            raise HTTPException(status_code=422, detail="选中导出必须提供邮箱 ID")
        return await resource_service.export_emails(payload)

    @app.get("/api/proxies", response_model=Page[ProxyRecord])
    async def list_proxies(
        page: PageNumber = 1,
        page_size: PageSizeOption = Query(PageSizeOption.TEN, alias="pageSize"),
        q: SearchQuery = "",
        country: Annotated[str, Query(max_length=2)] = "",
    ) -> Page[ProxyRecord]:
        mongo.require_online()
        return await resource_store.list_proxies(page, int(page_size), q, country)  # type: ignore[arg-type]

    @app.post("/api/proxies/import", response_model=ImportResult)
    async def import_proxies(payload: ProxyImportInput) -> ImportResult:
        mongo.require_online()
        return await resource_service.import_proxies(payload.rawText, payload.country, payload.group)

    @app.post(
        "/api/proxies/import-subscription",
        response_model=ProxySubscriptionImportResult,
    )
    async def import_proxy_subscription(
        payload: ProxySubscriptionImportInput,
    ) -> ProxySubscriptionImportResult:
        mongo.require_online()
        try:
            return await proxy_subscription_service.import_subscription(payload)
        except ProxySubscriptionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @app.get("/api/proxies/countries", response_model=list[ProxyCountrySummary])
    async def proxy_countries() -> list[ProxyCountrySummary]:
        mongo.require_online()
        return await resource_store.proxy_country_summaries()

    @app.post("/api/proxies/test", response_model=ProxyTestResult)
    async def test_proxies(payload: ProxyTestInput) -> ProxyTestResult:
        mongo.require_online()
        return await proxy_subscription_service.test_stored_proxies(
            country=payload.country,
            group=payload.group,
            timeout_seconds=payload.timeoutSeconds,
        )

    @app.get("/api/proxies/groups", response_model=list[ProxyGroupSummary])
    async def proxy_groups() -> list[ProxyGroupSummary]:
        mongo.require_online()
        return await resource_store.proxy_group_summaries()

    @app.patch("/api/proxies/groups")
    async def update_proxy_group(payload: ProxyGroupUpdate) -> dict[str, int]:
        mongo.require_online()
        return await resource_store.update_proxy_group(
            payload.country,
            payload.group,
            new_country=payload.newCountry,
            new_group=payload.newGroup,
            enabled=payload.enabled,
        )

    @app.delete("/api/proxies/groups", response_model=DeleteResult)
    async def delete_proxy_group(
        country: Annotated[str, Query(min_length=2, max_length=2)],
        group: Annotated[str, Query(min_length=1, max_length=64)],
    ) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_proxy_group(country, group)

    @app.post("/api/proxies/bulk-delete", response_model=DeleteResult)
    async def delete_proxies(payload: BulkIdsInput) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_proxies(payload.ids)

    @app.delete("/api/proxies", response_model=DeleteResult)
    async def clear_proxies() -> DeleteResult:
        mongo.require_online()
        return await resource_store.clear_proxies()

    @app.patch("/api/proxies/{proxy_id}", response_model=ProxyRecord)
    async def update_proxy(proxy_id: str, payload: ProxyUpdate) -> ProxyRecord:
        mongo.require_online()
        return await resource_store.update_proxy(
            proxy_id,
            enabled=payload.enabled,
            country=payload.country,
            group=payload.group,
        )

    @app.delete("/api/proxies/{proxy_id}", response_model=DeleteResult)
    async def delete_proxy(proxy_id: str) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_proxy(proxy_id)

    @app.get("/api/stats/overview", response_model=OverviewStats)
    async def overview_stats() -> OverviewStats:
        mongo.require_online()
        return await resource_store.overview_stats()

    @app.post("/api/runs/mock", response_model=RunState, status_code=202)
    async def start_mock_run(payload: MockRunCreate) -> RunState:
        try:
            return await run_manager.start(payload.count)
        except RunConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "run_conflict", "message": str(exc)},
            ) from exc
        except InsufficientEmailsError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "insufficient_emails", "message": str(exc)},
            ) from exc

    @app.post("/api/runs/browser-probe", response_model=RunState, status_code=202)
    async def start_browser_probe_run(payload: BrowserProbeRunCreate) -> RunState:
        try:
            return await run_manager.start_browser_probe(
                payload.count,
                payload.country,
                payload.group,
                payload.emailSource,
            )
        except RunConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "run_conflict", "message": str(exc)},
            ) from exc
        except InsufficientEmailsError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "insufficient_emails", "message": str(exc)},
            ) from exc
        except ProxyCountryUnavailableError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "proxy_country_unavailable",
                    "message": str(exc),
                    "country": exc.country,
                    "required": exc.required,
                    "available": exc.available,
                },
            ) from exc
        except LocalProxyUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "local_proxy_unavailable",
                    "message": str(exc),
                    "host": "127.0.0.1",
                    "port": 7890,
                },
            ) from exc
        except RoxyWorkspaceMissingError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "roxy_workspace_missing",
                    "message": str(exc),
                    "available": 0,
                },
            ) from exc
        except RoxyApiError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": (
                        "roxy_auth_failed"
                        if exc.is_auth_failure
                        else "roxy_not_ready"
                    ),
                    "message": (
                        "Roxy API 凭据无效"
                        if exc.is_auth_failure
                        else "Roxy 尚未达到稳定就绪状态"
                    ),
                },
            ) from exc
    @app.get("/api/runs/active", response_model=RunState | None)
    async def active_mock_run() -> RunState | None:
        return await run_manager.active()

    @app.get("/api/runs/{run_id}", response_model=RunState)
    async def get_mock_run(run_id: str) -> RunState:
        try:
            return await run_manager.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": str(exc)},
            ) from exc

    @app.get("/api/runs/{run_id}/workers", response_model=list[WorkerSnapshot])
    async def get_run_workers(run_id: str) -> list[WorkerSnapshot]:
        try:
            return await run_manager.workers(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": str(exc)},
            ) from exc

    @app.post("/api/runs/{run_id}/cancel", response_model=RunState)
    async def cancel_mock_run(run_id: str) -> RunState:
        try:
            return await run_manager.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": str(exc)},
            ) from exc

    @app.get("/api/run-logs/runs", response_model=list[RunLogSummary])
    def list_run_logs(request: Request) -> list[RunLogSummary]:
        try:
            return request.app.state.run_log_store.list_runs()
        except CorruptRunLogError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_corrupted", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_read_failed", "message": "无法读取任务日志目录"},
            ) from exc

    @app.get("/api/run-logs/runs/{run_id}", response_model=RunLogFile)
    def get_run_log(run_id: UUID, request: Request) -> RunLogFile:
        try:
            return request.app.state.run_log_store.get_run(run_id)
        except RunLogNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_log_not_found", "message": str(exc)},
            ) from exc
        except CorruptRunLogError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_corrupted", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_read_failed", "message": "无法读取任务日志文件"},
            ) from exc

    return app


app = create_app()
