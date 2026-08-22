from __future__ import annotations

import base64
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from unittest.mock import AsyncMock, Mock

from backend.account_rebind_service import (
    AccountRebindError,
    AccountRebindService,
    ChatGptWebSession,
    _access_token_expiry,
)
from backend.resource_models import AccountCreate
from backend.resource_service import MongoResourceStore


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.signature"


class _BranchSession(ChatGptWebSession):
    def __init__(self, *, simulated_mfa: str = "", **kwargs):
        self.simulated_mfa = simulated_mfa
        super().__init__(**kwargs)

    def _start(self) -> None:
        return None

    def _password_login(self) -> None:
        self.mfa_type_used = self.simulated_mfa

    def _email_login(self) -> None:
        self.mfa_type_used = self.simulated_mfa


@pytest.mark.parametrize(
    ("password", "totp", "mfa", "expected"),
    [
        ("secret", "", "", "password"),
        ("secret", "TOTPSECRET", "totp", "password_totp"),
        ("secret", "", "email_otp", "password_email"),
        ("", "", "", "email"),
        ("", "TOTPSECRET", "totp", "email_totp"),
    ],
)
def test_login_uses_available_credential_branch(password: str, totp: str, mfa: str, expected: str) -> None:
    token = _jwt(2_000_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={"accessToken": token, "user": {"email": "owner@example.com"}})
        if request.url.path == "/backend-api/me":
            assert request.headers["authorization"] == f"Bearer {token}"
            return httpx.Response(200, json={"email": "owner@example.com"})
        raise AssertionError(f"unexpected request: {request.url}")

    client = _BranchSession(
        email="owner@example.com",
        password=password,
        totp_secret=totp,
        email_access_url="https://mail.example.test/code",
        simulated_mfa=mfa,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.login()
    finally:
        client.close()

    assert result.branch == expected
    assert result.access_token == token
    assert result.access_token_expires_at == datetime.fromtimestamp(2_000_000_000, timezone.utc)


def test_login_rejects_session_and_me_identity_disagreement() -> None:
    token = _jwt(2_000_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={"accessToken": token, "user": {"email": "owner@example.com"}})
        return httpx.Response(200, json={"email": "different@example.com"})

    client = _BranchSession(
        email="owner@example.com",
        password="secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AccountRebindError, match="identity_check_email_mismatch"):
        client.login()
    client.close()


def test_password_only_login_falls_back_to_email_when_session_has_no_token() -> None:
    progress: list[tuple[str, int, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/session"
        return httpx.Response(200, json={"user": {"email": "owner@example.com"}})

    client = _BranchSession(
        email="owner@example.com",
        password="secret",
        email_access_url="https://mail.example.test/code",
        transport=httpx.MockTransport(handler),
        progress=lambda step, percent, message: progress.append((step, percent, message)),
    )
    try:
        with pytest.raises(AccountRebindError) as caught:
            client.login()
    finally:
        client.close()

    assert caught.value.code == "retry_with_email_login"
    assert caught.value.retryable is True
    assert progress[-1][0] == "login.email_fallback"


def test_wait_for_code_uses_dedicated_mailbox_request() -> None:
    client = ChatGptWebSession(
        email="owner@example.com",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    client._request = Mock(side_effect=AssertionError("mailbox request used ChatGPT transport"))
    client._mailbox_request = Mock(
        return_value=httpx.Response(
            200,
            json={
                "recipient": "owner@example.com",
                "sender": "noreply@tm.openai.com",
                "code": "123456",
            },
        )
    )
    try:
        code = client._wait_for_code(
            "http://127.0.0.1:3211/code/example",
            issued_after=0,
            timeout=5,
        )
    finally:
        client.close()

    assert code == "123456"
    client._mailbox_request.assert_called_once()


def test_access_token_expiry_is_resilient_to_opaque_tokens() -> None:
    assert _access_token_expiry("opaque-token") is None


def test_proxy_probe_classifies_cloudflare_before_account_work() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(403, text="challenge")
        return httpx.Response(403, text="challenge")

    service = AccountRebindService(transport=httpx.MockTransport(handler))
    with pytest.raises(AccountRebindError) as caught:
        service.probe_proxy("")
    assert caught.value.code == "cloudflare_challenge_required"
    assert caught.value.retryable is True


def test_account_model_accepts_missing_password_and_totp_for_email_login() -> None:
    account = AccountCreate(
        email="mail-only@example.com",
        emailAccessUrl="https://mail.example.test/code",
    )
    assert account.chatgptPassword == ""
    assert account.totpSecret == ""


def test_complete_rebind_confirms_new_session_and_returns_new_access_token() -> None:
    old_email = "old@example.com"
    new_email = "new@example.com"
    old_token = _jwt(2_000_000_000)
    new_token = _jwt(2_100_000_000)
    state = {"login_email": old_email, "verified": False, "device_ids": []}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "chatgpt.com" and path == "/auth/login":
            return httpx.Response(
                200,
                text="login",
                headers={"set-cookie": "shared=session-chatgpt; Domain=chatgpt.com; Path=/"},
            )
        if host == "chatgpt.com" and path == "/api/auth/csrf":
            return httpx.Response(200, json={"csrfToken": "csrf"})
        if host == "chatgpt.com" and path == "/api/auth/signin/openai":
            state["login_email"] = request.url.params["login_hint"]
            return httpx.Response(200, json={"url": "https://auth.openai.com/api/accounts/authorize"})
        if host == "auth.openai.com" and path == "/api/accounts/authorize":
            return httpx.Response(
                200,
                text="password",
                headers={"set-cookie": "shared=session-auth; Domain=auth.openai.com; Path=/"},
            )
        if host == "auth.openai.com" and path == "/api/accounts/password/verify":
            state["device_ids"].append(request.headers["oai-device-id"])
            return httpx.Response(200, json={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=one"})
        if host == "chatgpt.com" and path == "/api/auth/callback/openai":
            return httpx.Response(200, text="ok")
        if host == "chatgpt.com" and path == "/api/auth/session":
            email = str(state["login_email"])
            token = new_token if email == new_email else old_token
            return httpx.Response(200, json={"accessToken": token, "user": {"email": email}})
        if host == "chatgpt.com" and path == "/backend-api/me":
            return httpx.Response(200, json={"email": state["login_email"]})
        if path == "/backend-api/accounts/change_email/eligibility":
            return httpx.Response(200, json={"eligible": True, "eligibility_type": "password"})
        if path == "/backend-api/accounts/change_email/begin":
            assert json.loads(request.content) == {"email": new_email}
            return httpx.Response(200, json={"success": True})
        if host == "mail.example.test" and path == "/new-code":
            return httpx.Response(
                200,
                json={
                    "recipient": new_email,
                    "sender": "noreply@tm.openai.com",
                    "code": "123456",
                    "receivedAt": datetime.now(timezone.utc).isoformat(),
                },
            )
        if path == "/backend-api/accounts/change_email/verify":
            assert json.loads(request.content) == {"email": new_email, "code": "123456"}
            state["verified"] = True
            return httpx.Response(200, json={"success": True})
        if path == "/auth/logout":
            return httpx.Response(200, text="bye")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    service = AccountRebindService(transport=httpx.MockTransport(handler))
    changed: list[tuple[str, str]] = []
    result = service.rebind(
        {
            "email": old_email,
            "chatgptPassword": "password",
            "totpSecret": "",
            "emailAccessUrl": "https://mail.example.test/old-code",
        },
        {"email": new_email, "accessUrl": "https://mail.example.test/new-code"},
        email_changed=lambda old, new: changed.append((old, new)),
    )

    assert state["verified"] is True
    assert result.old_email == old_email
    assert result.new_email == new_email
    assert result.access_token == new_token
    assert result.login_branch == "password"
    assert result.confirmation_branch == "password"
    assert changed == [(old_email, new_email)]
    assert len(set(state["device_ids"])) == 1


def test_refresh_access_token_uses_current_account_email() -> None:
    token = _jwt(2_100_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return httpx.Response(200)
        if path == "/api/auth/csrf":
            return httpx.Response(200, json={"csrfToken": "csrf"})
        if path == "/api/auth/signin/openai":
            assert request.url.params["login_hint"] == "current@example.com"
            return httpx.Response(200, json={"url": "https://auth.openai.com/api/accounts/authorize"})
        if path == "/api/accounts/authorize":
            return httpx.Response(200)
        if path == "/api/accounts/password/verify":
            return httpx.Response(200, json={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=one"})
        if path == "/api/auth/callback/openai":
            return httpx.Response(200)
        if path == "/api/auth/session":
            return httpx.Response(200, json={"accessToken": token, "user": {"email": "current@example.com"}})
        if path == "/backend-api/me":
            return httpx.Response(200, json={"email": "current@example.com"})
        raise AssertionError(request.url)

    result = AccountRebindService(
        transport=httpx.MockTransport(handler)
    ).refresh_access_token({
        "email": "current@example.com",
        "chatgptPassword": "password",
        "emailAccessUrl": "https://mail.example.test/code",
    })

    assert result.email == "current@example.com"
    assert result.access_token == token


def test_rebind_mailbox_reservation_excludes_current_account_email() -> None:
    emails = Mock()
    emails.find_one_and_update = AsyncMock(return_value={"_id": "new-mail"})
    manager = Mock(database={"emails": emails})
    store = MongoResourceStore(manager)
    result = asyncio.run(store.reserve_rebind_email("run-1", "Owner@Example.com"))

    assert result == {"_id": "new-mail"}
    query = emails.find_one_and_update.call_args.args[0]
    assert {"emailNormalized": {"$ne": "owner@example.com"}} in query["$and"]
    assert {"email": {"$ne": "owner@example.com"}} in query["$and"]


def test_rebind_mailbox_reservation_can_select_mailcom_aliases() -> None:
    emails = Mock()
    emails.find_one_and_update = AsyncMock(return_value={"_id": "alias-mail"})
    manager = Mock(database={"emails": emails})
    store = MongoResourceStore(manager)

    result = asyncio.run(
        store.reserve_rebind_email("run-alias", "", source="mailcom_alias")
    )

    assert result == {"_id": "alias-mail"}
    query = emails.find_one_and_update.call_args.args[0]
    assert query["sourceType"] == "mailcom_alias"


def test_retry_can_reacquire_the_previously_assigned_mailbox() -> None:
    emails = Mock()
    emails.find_one_and_update = AsyncMock(
        return_value={"_id": "same-mail", "status": "reserved"}
    )
    manager = Mock(database={"emails": emails})
    store = MongoResourceStore(manager)

    result = asyncio.run(
        store.reserve_specific_rebind_email("same-mail", "rebind-run-account")
    )

    assert result["_id"] == "same-mail"
    query = emails.find_one_and_update.call_args.args[0]
    assert query["_id"] == "same-mail"
    assert {"status": "available"} in query["$or"]


def test_rebind_task_is_saved_and_removed_from_mongodb() -> None:
    tasks = Mock()
    tasks.update_one = AsyncMock()
    manager = Mock(database={"account_rebind_tasks": tasks})
    store = MongoResourceStore(manager)

    asyncio.run(store.save_rebind_task({
        "taskId": "task-1",
        "status": "running",
        "items": [{"accountId": "account-1", "status": "queued"}],
    }))
    asyncio.run(store.delete_rebind_task("task-1"))

    saved = tasks.update_one.await_args_list[0].args[1]["$set"]
    assert saved["taskId"] == "task-1"
    assert saved["items"][0]["status"] == "queued"
    assert tasks.update_one.await_count == 2
    tombstone = tasks.update_one.await_args_list[1].args[1]
    assert "deletedAt" in tombstone["$set"]
    assert tombstone["$unset"] == {"items": ""}
