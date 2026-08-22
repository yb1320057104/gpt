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
from .global_promotion_service import GlobalPromotionCheckService
from .account_alive_service import AccountAliveCheckService
from .account_alive_scheduler import AccountAliveScheduler
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
    ) -> Page[AccountRecord]:
        mongo.require_online()
        return await resource_store.list_accounts(
            page, int(page_size), q, promotion, country, alive, global_promotion, rebind
        )  # type: ignore[arg-type]

    @app.get("/api/account-rebind/pools")
    async def account_rebind_pools(request: Request) -> dict:
        mongo.require_online()
        available = await resource_store.emails.count_documents({"status": "available", "usagePurpose": {"$ne": "rebind"}})
        reserved = await resource_store.emails.count_documents({"status": "reserved", "usagePurpose": "rebind"})
        success = await resource_store.accounts.find({"rebindStatus": "success"}, {"email": 1, "reboundEmail": 1, "accessTokenConfigured": 1, "rebindProxy": 1}).sort("updatedAt", -1).to_list(length=100)
        return {"availableRegistrationEmails": await resource_store.emails.count_documents({"status": "available", "$or": [{"usagePurpose": {"$exists": False}}, {"usagePurpose": "registration"}]}), "availableRebindEmails": available, "reservedRebindEmails": reserved, "proxy": request.app.state.account_rebind_proxy, "success": [{"id": str(item.get("_id")), **{k: item.get(k) for k in ("email", "reboundEmail", "accessTokenConfigured", "rebindProxy")}} for item in success]}

    @app.put("/api/account-rebind/proxy")
    async def set_account_rebind_proxy(request: Request) -> dict:
        payload = await request.json()
        proxy = str(payload.get("proxy") or "").strip()
        if proxy and not urlparse(proxy).scheme:
            raise HTTPException(status_code=422, detail="代理地址必须包含协议，例如 http://")
        request.app.state.account_rebind_proxy = proxy
        return {"ok": True, "proxy": proxy}

    @app.get("/api/account-rebind/tasks")
    async def account_rebind_tasks(request: Request) -> dict:
        return {"items": list(request.app.state.account_rebind_tasks.values())}

    @app.get("/api/account-rebind/logs")
    async def account_rebind_logs(request: Request) -> dict:
        return {"items": request.app.state.account_rebind_logs[-300:]}

    @app.put("/api/account-rebind/concurrency")
    async def set_account_rebind_concurrency(request: Request) -> dict:
        payload = await request.json()
        value = max(1, min(20, int(payload.get("concurrency") or 1)))
        request.app.state.account_rebind_concurrency = value
        return {"concurrency": value}

    @app.get("/api/account-rebind/proxies")
    async def account_rebind_proxies() -> dict:
        mongo.require_online()
        docs = await resource_store.proxies.find({"enabled": {"$ne": False}, "status": {"$in": ["available", "unknown"]}}, {"_id": 1, "country": 1, "group": 1, "scheme": 1, "host": 1, "port": 1}).sort("country", 1).to_list(length=500)
        items = [{"id": "local7890", "label": "本地代理 · 127.0.0.1:7890", "value": "http://127.0.0.1:7890", "source": "local"}]
        items.extend({"id": str(doc["_id"]), "label": f"{doc.get('country', 'ZZ')} · {doc.get('group', 'default')} · {doc.get('host')}:{doc.get('port')}", "value": "", "source": "pool"} for doc in docs)
        return {"items": items}

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
            items.append({"accountId": str(account["_id"]), "email": account.get("email", ""), "status": "pending", "progress": 0, "mailbox": "", "mailboxId": "", "error": "", "runId": f"rebind-{task_id}-{account['_id']}"})
        task = {"taskId": task_id, "status": "pending", "progress": 0, "proxy": proxy, "proxyId": "", "createdAt": datetime.now(timezone.utc).isoformat(), "items": items, "message": "等待选择代理并开始换绑"}
        request.app.state.account_rebind_tasks[task_id] = task
        request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO", "message": f"任务 {task_id[:8]} 已创建，共 {len(items)} 个账号"})
        return task

    @app.post("/api/account-rebind/tasks/{task_id}/start")
    async def start_account_rebind_task(task_id: str, request: Request) -> dict:
        task = request.app.state.account_rebind_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="换绑任务不存在")
        payload = await request.json()
        proxy_id = str(payload.get("proxyId") or "").strip()
        custom_proxy = str(payload.get("proxy") or "").strip()
        if proxy_id == "local7890":
            proxy = "http://127.0.0.1:7890"
        elif proxy_id:
            doc = await resource_store.proxies.find_one({"_id": proxy_id, "enabled": {"$ne": False}})
            if not doc:
                raise HTTPException(status_code=422, detail="所选代理不存在或已禁用")
            auth = f"{doc.get('username')}:{doc.get('password')}@" if doc.get("username") else ""
            proxy = f"{doc.get('scheme', 'http')}://{auth}{doc.get('host')}:{doc.get('port')}"
        else:
            proxy = custom_proxy
        if not proxy:
            raise HTTPException(status_code=422, detail="请选择代理池代理、本地代理或填写自定义代理")
        task["proxy"], task["proxyId"], task["status"], task["message"] = proxy, proxy_id, "queued", "邮箱已分配，等待换绑执行器"
        request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO", "taskId": task_id, "step": "proxy.selected", "message": f"任务 {task_id[:8]} 已选择代理 {proxy}"})
        for item in task.get("items", []):
            request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO", "taskId": task_id, "accountId": item["accountId"], "step": "mailbox.reserve", "message": f"账号 {item['email']} 正在申请换绑邮箱"})
            mailbox = await resource_store.reserve_rebind_email(item["runId"])
            item.update({"status": "queued" if mailbox else "failed", "progress": 5 if mailbox else 0, "mailbox": mailbox.get("email") if mailbox else "", "mailboxId": str(mailbox.get("_id")) if mailbox else "", "error": "" if mailbox else "rebind_mailbox_empty"})
            request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO" if mailbox else "ERROR", "message": f"账号 {item['email']} {'占用邮箱 ' + item['mailbox'] if mailbox else '分配邮箱失败'}"})
        task["progress"] = min((item.get("progress", 0) for item in task["items"]), default=0)
        endpoint = os.getenv("CHATGPT_EMAIL_CHANGE_ENDPOINT", "").strip()
        if endpoint:
            request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO", "taskId": task_id, "step": "email_change.ready", "message": f"准备调用 POST {endpoint}"})
        else:
            task["status"], task["message"] = "waiting_contract", "换绑接口未配置，未发送请求"
            request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "WARN", "taskId": task_id, "step": "email_change.blocked", "message": "CHATGPT_EMAIL_CHANGE_ENDPOINT 未配置，未调用任何远程接口"})
        return task

    @app.post("/api/account-rebind/tasks/start")
    async def start_pending_account_rebind_tasks(request: Request) -> dict:
        pending = [(task_id, task) for task_id, task in request.app.state.account_rebind_tasks.items() if task.get("status") == "pending"]
        concurrency = request.app.state.account_rebind_concurrency
        semaphore = asyncio.Semaphore(concurrency)
        async def run_one(task_id: str) -> dict:
            async with semaphore:
                return await start_account_rebind_task(task_id, request)
        results = await asyncio.gather(*(run_one(task_id) for task_id, _ in pending), return_exceptions=True)
        started = sum(not isinstance(result, Exception) for result in results)
        request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "INFO", "step": "batch.start", "message": f"批量开始 {started}/{len(pending)} 个任务，并发 {concurrency}"})
        return {"requested": len(pending), "started": started, "concurrency": concurrency}

    @app.post("/api/account-rebind/tasks/{task_id}/release")
    async def release_account_rebind_task(task_id: str, request: Request) -> dict:
        task = request.app.state.account_rebind_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="换绑任务不存在")
        for item in task.get("items", []):
            if item.get("mailboxId"):
                await resource_store.release_rebind_email(item["mailboxId"], item.get("runId", ""))
                item["status"] = "released"
        task["status"] = "released"
        task["message"] = "任务已取消，占用邮箱已释放"
        request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "WARN", "message": f"任务 {task_id[:8]} 已释放"})
        return task

    @app.post("/api/account-rebind/tasks/cancel-all")
    async def cancel_all_account_rebind_tasks(request: Request) -> dict:
        cancelled = 0
        for task_id, task in list(request.app.state.account_rebind_tasks.items()):
            if task.get("status") in {"success", "released", "failed"}:
                continue
            for item in task.get("items", []):
                if item.get("mailboxId"):
                    await resource_store.release_rebind_email(item["mailboxId"], item.get("runId", ""))
                item["status"] = "cancelled"
            task["status"] = "cancelled"
            task["message"] = "任务已取消，占用邮箱已释放"
            cancelled += 1
        request.app.state.account_rebind_logs.append({"time": datetime.now(timezone.utc).isoformat(), "level": "WARN", "step": "batch.cancel", "message": f"一键取消任务 {cancelled} 个"})
        return {"cancelled": cancelled}

    @app.post("/api/accounts", response_model=AccountRecord, status_code=201)
    async def create_account(payload: AccountCreate) -> AccountRecord:
        mongo.require_online()
        try:
            return await resource_service.create_account(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
