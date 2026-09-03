from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests.errors import RequestsError as CurlRequestException


CHATGPT = "https://chatgpt.com"
AUTH = "https://auth.openai.com"
CHANGE_EMAIL_ELIGIBILITY = f"{CHATGPT}/backend-api/accounts/change_email/eligibility"
CHANGE_EMAIL_BEGIN = f"{CHATGPT}/backend-api/accounts/change_email/begin"
CHANGE_EMAIL_VERIFY = f"{CHATGPT}/backend-api/accounts/change_email/verify"
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SENSITIVE_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"(?:https?|socks5h?)://[^@\s/]+@)",
    re.I,
)


class AccountRebindError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class LoginSession:
    email: str
    access_token: str
    branch: str
    access_token_expires_at: datetime | None = None


@dataclass(slots=True)
class RebindResult:
    old_email: str
    new_email: str
    access_token: str
    login_branch: str
    confirmation_branch: str
    access_token_expires_at: datetime | None = None


def _safe_error(value: Any) -> str:
    text = SENSITIVE_RE.sub("[REDACTED]", str(value or ""))
    return text[:240]


def _access_token_expiry(token: str) -> datetime | None:
    """Read the JWT expiry without logging or otherwise exposing the token."""
    try:
        part = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8")
        )
        expiry = int(claims.get("exp") or 0)
        return datetime.fromtimestamp(expiry, timezone.utc) if expiry > 0 else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _totp_now(secret: str) -> str:
    normalized = re.sub(r"\s+", "", secret).upper()
    try:
        key = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    except Exception as exc:
        raise AccountRebindError("totp_secret_invalid") from exc
    digest = hmac.new(
        key,
        struct.pack(">Q", int(time.time() // 30)),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _json(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _find_value(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in keys and isinstance(item, (str, int)):
                return str(item)
            nested = _find_value(item, keys)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_value(item, keys)
            if nested:
                return nested
    return ""


def _callback_url(value: Any) -> str:
    candidate = _find_value(
        value,
        {
            "continue_url",
            "continueurl",
            "callback_url",
            "callbackurl",
            "redirect_url",
            "redirecturl",
            "url",
        },
    )
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "auth.openai.com"}:
        return ""
    return candidate


def _factors(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        value = payload.get("factors")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        for item in payload.values():
            found = _factors(item)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _factors(item)
            if found:
                return found
    return []


def _is_mfa(payload: Any) -> bool:
    return "mfa_challenge" in json.dumps(payload or {}, ensure_ascii=False).casefold()


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _code_candidates(payload: Any, inherited_time: float | None = None) -> list[tuple[str, float | None]]:
    results: list[tuple[str, float | None]] = []
    if isinstance(payload, dict):
        own_time = inherited_time
        for key in ("received_at", "receivedAt", "created_at", "createdAt", "timestamp", "time"):
            if key in payload:
                own_time = _timestamp(payload.get(key)) or own_time
                break
        for key in ("verification_code", "verificationCode", "otp", "code"):
            match = CODE_RE.search(str(payload.get(key) or ""))
            if match:
                results.append((match.group(1), own_time))
        for key, value in payload.items():
            if key not in {"verification_code", "verificationCode", "otp", "code"}:
                results.extend(_code_candidates(value, own_time))
    elif isinstance(payload, list):
        for value in payload:
            results.extend(_code_candidates(value, inherited_time))
    elif isinstance(payload, str):
        match = CODE_RE.search(payload)
        if match:
            results.append((match.group(1), inherited_time))
    return results


def _metadata_values(payload: Any, keys: set[str]) -> list[str]:
    values: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).casefold() in keys and isinstance(value, (str, int)):
                values.append(str(value).strip().casefold())
            elif isinstance(value, (dict, list)):
                values.extend(_metadata_values(value, keys))
    elif isinstance(payload, list):
        for value in payload:
            values.extend(_metadata_values(value, keys))
    return values


class ChatGptWebSession:
    def __init__(
        self,
        *,
        email: str,
        password: str = "",
        totp_secret: str = "",
        email_access_url: str = "",
        proxy: str = "",
        sentinel_token: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        progress: Callable[[str, int, str], None] | None = None,
        device_id: str = "",
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.email = email.strip().lower()
        self.password = password
        self.totp_secret = re.sub(r"\s+", "", totp_secret).upper()
        self.email_access_url = email_access_url.strip()
        self.sentinel_token = sentinel_token.strip()
        self.progress = progress or (lambda _step, _percent, _message: None)
        self.device_id = device_id.strip() or str(uuid.uuid4())
        self.flow_id = str(uuid.uuid4())
        self.mfa_type_used = ""
        headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        self._uses_curl = transport is None
        if self._uses_curl:
            # Keep the TLS fingerprint and declared browser version aligned.
            # A generic moving Chrome fingerprint paired with a fixed Chrome
            # 131 UA is internally inconsistent and increases challenge risk.
            self.http = CurlSession(impersonate="chrome131", headers=headers)
            if proxy.strip():
                self.http.proxies = {"http": proxy.strip(), "https": proxy.strip()}
            # Mailbox access URLs are control-plane endpoints (frequently the
            # local MailCom service). They must never inherit the ChatGPT exit
            # proxy, otherwise 127.0.0.1 is resolved from the proxy server.
            self.mailbox_http = httpx.Client(
                follow_redirects=True,
                # MailCom may need to rotate several IMAP proxies before a
                # request completes. Keep this below the outer OTP deadline.
                timeout=max(timeout, 180.0),
                trust_env=False,
                headers=headers,
            )
            self.timeout = timeout
        else:
            # Unit tests and deterministic protocol simulations use MockTransport.
            self.http = httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                proxy=proxy.strip() or None,
                transport=transport,
                headers=headers,
            )
            # Mock transports are shared in protocol tests. Production curl
            # sessions always use the separate direct client above.
            self.mailbox_http = self.http
            self.timeout = timeout
        if cookies:
            self.http.cookies.update(cookies)

    def close(self) -> None:
        self.http.close()
        if self.mailbox_http is not self.http:
            self.mailbox_http.close()

    def _auth_headers(self, referer: str = f"{AUTH}/log-in/password") -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": AUTH,
            "Referer": referer,
            "oai-device-id": self.device_id,
            "x-access-flow-invocation-id": self.flow_id,
            "x-openai-document-navigation-id": self.flow_id,
        }
        if self.sentinel_token:
            headers["openai-sentinel-token"] = self.sentinel_token
        return headers

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._uses_curl:
                kwargs.setdefault("timeout", self.timeout)
                kwargs.setdefault("allow_redirects", True)
            return self.http.request(method, url, **kwargs)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            CurlRequestException,
            OSError,
        ) as exc:
            raise AccountRebindError(
                f"network_error:{type(exc).__name__}", retryable=True
            ) from None

    def _mailbox_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Access mailbox APIs directly, independently from the ChatGPT proxy."""
        try:
            return self.mailbox_http.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            raise AccountRebindError(
                f"mailbox_network_error:{type(exc).__name__}", retryable=True
            ) from None

    def _start(self) -> None:
        self.progress("login.csrf", 10, "正在初始化登录会话")
        landing = self._request("GET", f"{CHATGPT}/auth/login")
        if landing.status_code in {401, 403}:
            raise AccountRebindError("cloudflare_challenge_required", retryable=True)
        csrf_response = self._request(
            "GET", f"{CHATGPT}/api/auth/csrf", headers={"Accept": "application/json"}
        )
        csrf = str(_json(csrf_response).get("csrfToken") or "")
        if not csrf:
            if csrf_response.status_code in {401, 403}:
                raise AccountRebindError("cloudflare_challenge_required", retryable=True)
            raise AccountRebindError(
                f"csrf_missing_http_{csrf_response.status_code}", retryable=True
            )
        self.http.cookies.set("oai-did", self.device_id, domain="chatgpt.com", path="/")
        signin = self._request(
            "POST",
            f"{CHATGPT}/api/auth/signin/openai",
            params={
                "prompt": "login",
                "screen_hint": "login_or_signup",
                "ext-oai-did": self.device_id,
                "auth_session_logging_id": self.flow_id,
                "login_hint": self.email,
            },
            data={"callbackUrl": f"{CHATGPT}/", "csrfToken": csrf, "json": "true"},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": CHATGPT,
                "Referer": f"{CHATGPT}/auth/login",
            },
        )
        auth_url = str(_json(signin).get("url") or signin.headers.get("location") or "")
        if not auth_url:
            if signin.status_code in {401, 403}:
                raise AccountRebindError("cloudflare_challenge_required", retryable=True)
            raise AccountRebindError(f"signin_failed_http_{signin.status_code}")
        authorize = self._request("GET", auth_url)
        if authorize.status_code in {401, 403}:
            raise AccountRebindError("cloudflare_challenge_required", retryable=True)
        if authorize.status_code >= 400:
            raise AccountRebindError(f"authorize_http_{authorize.status_code}")

    def _wait_for_code(
        self,
        access_url: str,
        *,
        issued_after: float,
        timeout: int = 180,
        expected_email: str = "",
    ) -> str:
        if not access_url:
            raise AccountRebindError("email_access_url_missing")
        deadline = time.monotonic() + max(5, timeout)
        self.progress("email.wait", 42, "正在等待邮箱验证码")
        while time.monotonic() < deadline:
            response = self._mailbox_request(
                "GET",
                access_url,
                params={"wait": min(10, max(1, int(deadline - time.monotonic())))},
            )
            try:
                payload: Any = response.json()
            except ValueError:
                payload = response.text
            expected = (expected_email or self.email).strip().casefold()
            recipients = _metadata_values(
                payload,
                {"to", "to_email", "toemail", "recipient", "recipient_email", "email"},
            )
            if recipients and not any(expected in value for value in recipients):
                time.sleep(2)
                continue
            senders = _metadata_values(
                payload,
                {"from", "from_email", "fromemail", "sender", "sender_email"},
            )
            if senders and not any("openai" in value for value in senders):
                time.sleep(2)
                continue
            candidates = _code_candidates(payload)
            for code, received_at in candidates:
                if received_at is None or received_at >= issued_after - 2:
                    return code
            time.sleep(2)
        raise AccountRebindError("email_code_timeout", retryable=True)

    def _follow_callback(self, payload: dict[str, Any]) -> None:
        callback = _callback_url(payload)
        if not callback:
            raise AccountRebindError("oauth_callback_missing")
        response = self._request("GET", callback)
        if response.status_code >= 400:
            raise AccountRebindError(f"oauth_callback_http_{response.status_code}")

    def _complete_mfa(self, payload: dict[str, Any]) -> None:
        factors = _factors(payload)
        selected: dict[str, Any] | None = None
        if self.totp_secret:
            selected = next(
                (item for item in factors if "totp" in str(item.get("factor_type") or item.get("type") or "").casefold()),
                None,
            )
        if selected is None and self.email_access_url:
            selected = next(
                (
                    item
                    for item in factors
                    if "email" in str(item.get("factor_type") or item.get("type") or "").casefold()
                    and "totp" not in str(item.get("factor_type") or item.get("type") or "").casefold()
                ),
                None,
            )
        if selected is None:
            raise AccountRebindError("mfa_credentials_missing")
        factor_id = str(selected.get("id") or selected.get("factor_id") or "")
        factor_type = str(selected.get("factor_type") or selected.get("type") or "totp")
        if not factor_id:
            raise AccountRebindError("mfa_factor_missing")
        self.mfa_type_used = factor_type.casefold()
        self.progress("login.mfa", 30, f"正在验证 {factor_type.upper()}")
        issued_at = time.time()
        issue = self._request(
            "POST",
            f"{AUTH}/api/accounts/mfa/issue_challenge",
            json={"id": factor_id, "type": factor_type, "force_fresh_challenge": False},
            headers=self._auth_headers(),
        )
        if issue.status_code >= 400:
            raise AccountRebindError(f"mfa_issue_http_{issue.status_code}")
        code = (
            _totp_now(self.totp_secret)
            if "totp" in factor_type.casefold()
            else self._wait_for_code(self.email_access_url, issued_after=issued_at)
        )
        verified = self._request(
            "POST",
            f"{AUTH}/api/accounts/mfa/verify",
            json={"id": factor_id, "type": factor_type, "code": code},
            headers=self._auth_headers(f"{AUTH}/mfa-challenge/{factor_id}"),
        )
        if verified.status_code >= 400:
            raise AccountRebindError(f"mfa_verify_http_{verified.status_code}")
        self._follow_callback(_json(verified))

    def _password_login(self) -> None:
        self.progress("login.password", 20, "正在验证账号密码")
        response = self._request(
            "POST",
            f"{AUTH}/api/accounts/password/verify",
            json={"password": self.password},
            headers=self._auth_headers(),
        )
        payload = _json(response)
        if response.status_code >= 400:
            raise AccountRebindError(f"password_verify_http_{response.status_code}")
        if _is_mfa(payload):
            self._complete_mfa(payload)
            return
        self._follow_callback(payload)

    def _send_email_otp(self) -> float:
        headers = self._auth_headers(f"{AUTH}/email-verification")
        issued_at = time.time()
        last_status = 0
        for path in (
            "/api/accounts/passwordless/send-otp",
            "/api/accounts/email-otp/send",
            "/api/accounts/email-otp/resend",
        ):
            response = self._request("POST", f"{AUTH}{path}", json={}, headers=headers)
            last_status = response.status_code
            if response.status_code in {200, 202, 204}:
                return issued_at
            if response.status_code == 409:
                body = json.dumps(_json(response), ensure_ascii=False).casefold()
                if any(marker in body for marker in ("already", "pending", "rate", "too_many")):
                    return issued_at
            if response.status_code not in {400, 404, 405, 409}:
                break
        raise AccountRebindError(f"email_otp_issue_http_{last_status}")

    def _validate_email_otp(self, code: str) -> dict[str, Any]:
        headers = self._auth_headers(f"{AUTH}/email-verification")
        attempts = [
            ("/api/accounts/email-otp/validate", {"code": code}),
            ("/api/accounts/email-verification/validate", {"code": code}),
            ("/api/accounts/email-verification/verify", {"code": code}),
            ("/api/accounts/verify-email", {"code": code}),
        ]
        last_status = 0
        for path, payload in attempts:
            response = self._request("POST", f"{AUTH}{path}", json=payload, headers=headers)
            last_status = response.status_code
            if response.status_code == 200:
                return _json(response)
            if response.status_code not in {404, 405}:
                break
        raise AccountRebindError(f"email_otp_verify_http_{last_status}")

    def _email_login(self) -> None:
        self.progress("login.email", 20, "正在使用原邮箱验证码登录")
        continued = self._request(
            "POST",
            f"{AUTH}/api/accounts/authorize/continue",
            json={"username": {"value": self.email, "kind": "email"}},
            headers=self._auth_headers(f"{AUTH}/log-in"),
        )
        if continued.status_code not in {200, 400, 409}:
            raise AccountRebindError(f"authorize_continue_http_{continued.status_code}")
        continued_payload = _json(continued)
        if _callback_url(continued_payload) and not _is_mfa(continued_payload):
            self._follow_callback(continued_payload)
            return
        issued_at = self._send_email_otp()
        code = self._wait_for_code(self.email_access_url, issued_after=issued_at)
        payload = self._validate_email_otp(code)
        if _is_mfa(payload):
            self._complete_mfa(payload)
        else:
            self._follow_callback(payload)

    def login(self) -> LoginSession:
        if not self.email:
            raise AccountRebindError("account_email_missing")
        if not self.password and not self.email_access_url:
            raise AccountRebindError("account_login_credentials_missing")
        self._start()
        branch = "password"
        if self.password:
            try:
                self._password_login()
                branch = (
                    "password_totp"
                    if "totp" in self.mfa_type_used
                    else "password_email"
                    if "email" in self.mfa_type_used
                    else "password"
                )
            except AccountRebindError as exc:
                if exc.code != "mfa_credentials_missing" or not self.email_access_url:
                    raise
                raise AccountRebindError("retry_with_email_login") from exc
        else:
            self._email_login()
            branch = "email_totp" if "totp" in self.mfa_type_used else "email"
        session_response = None
        payload: dict[str, Any] = {}
        token = ""
        # The callback can finish a little before NextAuth exposes the session.
        # Poll briefly instead of treating an HTTP 200 without a token as a
        # permanent account failure.
        for attempt in range(5):
            session_response = self._request(
                "GET", f"{CHATGPT}/api/auth/session", headers={"Accept": "application/json"}
            )
            payload = _json(session_response)
            token = str(payload.get("accessToken") or "")
            if session_response.status_code != 200 or token:
                break
            if attempt < 4:
                time.sleep(0.5)
        assert session_response is not None
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        authenticated_email = str(user.get("email") or "").strip().lower()
        if session_response.status_code >= 400 or not token:
            # Password-only accounts can receive a successful password response
            # without a usable NextAuth session. Retry the same account through
            # its email OTP branch when a mailbox capability is available.
            if (
                session_response.status_code == 200
                and not token
                and self.password
                and self.email_access_url
                and not self.mfa_type_used
            ):
                self.progress(
                    "login.email_fallback",
                    22,
                    "密码登录未建立会话，正在切换原邮箱验证码登录",
                )
                raise AccountRebindError("retry_with_email_login", retryable=True)
            if session_response.status_code == 200 and not token:
                raise AccountRebindError("auth_session_token_missing", retryable=True)
            raise AccountRebindError(f"auth_session_http_{session_response.status_code}")
        if authenticated_email != self.email:
            raise AccountRebindError("authenticated_email_mismatch")
        me_response = self._request(
            "GET",
            f"{CHATGPT}/backend-api/me",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Origin": CHATGPT,
                "Referer": f"{CHATGPT}/",
            },
        )
        me_email = str(_json(me_response).get("email") or "").strip().lower()
        if me_response.status_code >= 400:
            raise AccountRebindError(f"identity_check_http_{me_response.status_code}")
        if me_email != self.email:
            raise AccountRebindError("identity_check_email_mismatch")
        self.progress("login.success", 40, f"已通过 {branch} 登录")
        return LoginSession(
            email=authenticated_email,
            access_token=token,
            branch=branch,
            access_token_expires_at=_access_token_expiry(token),
        )

    def change_email(self, login: LoginSession, new_email: str, access_url: str) -> None:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {login.access_token}",
            "Origin": CHATGPT,
            "Referer": f"{CHATGPT}/",
        }
        self.progress("rebind.eligibility", 48, "正在检查换绑资格")
        eligibility = self._request("GET", CHANGE_EMAIL_ELIGIBILITY, headers=headers)
        eligibility_data = _json(eligibility)
        if eligibility.status_code >= 400:
            raise AccountRebindError(f"eligibility_http_{eligibility.status_code}")
        if eligibility_data.get("eligible") is not True:
            kind = re.sub(r"[^a-z0-9_-]+", "_", str(eligibility_data.get("eligibility_type") or "unknown").casefold())
            raise AccountRebindError(f"email_change_ineligible:{kind}")
        self.progress("rebind.begin", 55, "正在向新邮箱发送换绑验证码")
        issued_at = time.time()
        begin = self._request(
            "POST", CHANGE_EMAIL_BEGIN, json={"email": new_email}, headers=headers
        )
        if begin.status_code >= 400 or _json(begin).get("success") is not True:
            raise AccountRebindError(f"email_change_begin_http_{begin.status_code}")
        code = self._wait_for_code(
            access_url,
            issued_after=issued_at,
            timeout=240,
            expected_email=new_email,
        )
        self.progress("rebind.verify", 72, "正在确认新邮箱验证码")
        verified = self._request(
            "POST",
            CHANGE_EMAIL_VERIFY,
            json={"email": new_email, "code": code},
            headers=headers,
        )
        if verified.status_code >= 400 or _json(verified).get("success") is not True:
            raise AccountRebindError(f"email_change_verify_http_{verified.status_code}")
        self._request("GET", f"{CHATGPT}/auth/logout")


class AccountRebindService:
    def __init__(
        self,
        *,
        sentinel_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.sentinel_token = (
            os.getenv("CHATGPT_SENTINEL_TOKEN", "")
            if sentinel_token is None
            else sentinel_token
        )
        self.transport = transport

    def probe_proxy(self, proxy: str) -> None:
        """Verify the selected exit can reach ChatGPT before reserving mailboxes."""
        client = ChatGptWebSession(
            email="proxy-probe@invalid.local",
            proxy=proxy,
            sentinel_token=self.sentinel_token,
            transport=self.transport,
        )
        try:
            client.progress("login.csrf", 0, "")
            client._request("GET", f"{CHATGPT}/auth/login")
            response = client._request(
                "GET", f"{CHATGPT}/api/auth/csrf", headers={"Accept": "application/json"}
            )
            if response.status_code in {401, 403}:
                raise AccountRebindError("cloudflare_challenge_required", retryable=True)
            if not str(_json(response).get("csrfToken") or ""):
                raise AccountRebindError(
                    f"csrf_missing_http_{response.status_code}", retryable=True
                )
        finally:
            client.close()

    def _session(
        self,
        account: dict[str, Any],
        *,
        email: str,
        access_url: str,
        proxy: str,
        progress: Callable[[str, int, str], None],
        force_email: bool = False,
        device_id: str = "",
        cookies: dict[str, str] | None = None,
    ) -> ChatGptWebSession:
        return ChatGptWebSession(
            email=email,
            password="" if force_email else str(account.get("chatgptPassword") or ""),
            totp_secret=str(account.get("totpSecret") or ""),
            email_access_url=access_url,
            proxy=proxy,
            sentinel_token=self.sentinel_token,
            transport=self.transport,
            progress=progress,
            device_id=device_id,
            cookies=cookies,
        )

    def _login(
        self,
        account: dict[str, Any],
        *,
        email: str,
        access_url: str,
        proxy: str,
        progress: Callable[[str, int, str], None],
        device_id: str = "",
        cookies: dict[str, str] | None = None,
    ) -> tuple[ChatGptWebSession, LoginSession]:
        client = self._session(
            account,
            email=email,
            access_url=access_url,
            proxy=proxy,
            progress=progress,
            device_id=device_id,
            cookies=cookies,
        )
        try:
            return client, client.login()
        except AccountRebindError as exc:
            client.close()
            can_fallback_to_email = bool(access_url) and exc.code in {
                "retry_with_email_login",
                "password_verify_http_400",
                "password_verify_http_401",
            }
            if not can_fallback_to_email:
                raise
        fallback = self._session(
            account,
            email=email,
            access_url=access_url,
            proxy=proxy,
            progress=progress,
            force_email=True,
            device_id=device_id,
            cookies=cookies,
        )
        try:
            return fallback, fallback.login()
        except Exception:
            fallback.close()
            raise

    def rebind(
        self,
        account: dict[str, Any],
        mailbox: dict[str, Any],
        *,
        proxy: str = "",
        progress: Callable[[str, int, str], None] | None = None,
        email_changed: Callable[[str, str], None] | None = None,
    ) -> RebindResult:
        notify = progress or (lambda _step, _percent, _message: None)
        old_email = str(account.get("email") or "").strip().lower()
        old_access_url = str(account.get("emailAccessUrl") or "").strip()
        new_email = str(mailbox.get("email") or "").strip().lower()
        new_access_url = str(mailbox.get("accessUrl") or "").strip()
        if not new_email or not new_access_url:
            raise AccountRebindError("reserved_mailbox_credentials_missing")
        device_id = str(uuid.uuid4())
        first, login = self._login(
            account,
            email=old_email,
            access_url=old_access_url,
            proxy=proxy,
            progress=notify,
            device_id=device_id,
        )
        cookie_snapshot: dict[str, str] = {}
        try:
            first.change_email(login, new_email, new_access_url)
            # Cookie containers may hold the same name for chatgpt.com and
            # auth.openai.com. Mapping-style ``items()`` raises CookieConflict
            # in that valid situation, so iterate the underlying jar instead.
            jar = getattr(first.http.cookies, "jar", first.http.cookies)
            cookie_snapshot = {
                str(cookie.name): str(cookie.value)
                for cookie in jar
                if getattr(cookie, "name", None)
            }
        finally:
            first.close()
        if email_changed is not None:
            email_changed(old_email, new_email)
        notify("confirm.login", 80, "正在使用新邮箱重新登录确认")
        confirmation, confirmed = self._login(
            account,
            email=new_email,
            access_url=new_access_url,
            proxy=proxy,
            progress=notify,
            device_id=device_id,
            cookies=cookie_snapshot,
        )
        confirmation.close()
        if confirmed.email != new_email:
            raise AccountRebindError("rebind_confirmation_email_mismatch")
        notify("complete", 100, "邮箱换绑并重新登录确认成功")
        return RebindResult(
            old_email=old_email,
            new_email=new_email,
            access_token=confirmed.access_token,
            login_branch=login.branch,
            confirmation_branch=confirmed.branch,
            access_token_expires_at=confirmed.access_token_expires_at,
        )

    def refresh_access_token(
        self,
        account: dict[str, Any],
        *,
        proxy: str = "",
        progress: Callable[[str, int, str], None] | None = None,
    ) -> LoginSession:
        """Log in with the account's current credentials and return a fresh AT."""
        notify = progress or (lambda _step, _percent, _message: None)
        email = str(account.get("email") or "").strip().lower()
        access_url = str(account.get("emailAccessUrl") or "").strip()
        client, login = self._login(
            account,
            email=email,
            access_url=access_url,
            proxy=proxy,
            progress=notify,
            device_id=str(uuid.uuid4()),
        )
        client.close()
        return login


__all__ = [
    "AccountRebindError",
    "AccountRebindService",
    "ChatGptWebSession",
    "LoginSession",
    "RebindResult",
    "_safe_error",
    "_access_token_expiry",
]
