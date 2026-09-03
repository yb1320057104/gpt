from __future__ import annotations

import json
import base64
import hashlib
import hmac
import logging
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx


CHATGPT = "https://chatgpt.com"
AUTH = "https://auth.openai.com"


class ApiLoginError(RuntimeError):
    pass


@dataclass(slots=True)
class ApiConfig:
    email: str
    password: str
    totp_secret: str = ""
    proxy: str = ""
    sentinel_token: str = ""
    timeout: float = 30.0


def safe_url(value: str) -> str:
    p = urlsplit(str(value))
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def safe_query(value: str) -> list[str]:
    return sorted(parse_qs(urlsplit(str(value)).query).keys())


def shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return None
    return type(value).__name__


def totp_now(secret: str) -> str:
    """Generate RFC 6238 SHA-1 TOTP without an extra runtime dependency."""
    normalized = re.sub(r"\s+", "", secret).upper()
    key = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    counter = int(time.time() // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


class ChatGPTApiClient:
    def __init__(self, config: ApiConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.log = logger or logging.getLogger("chatgpt-api")
        proxy = config.proxy.strip() or None
        self.http = httpx.Client(
            follow_redirects=True,
            timeout=config.timeout,
            proxy=proxy,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
        )
        self.device_id = str(uuid.uuid4())
        self.flow_id = str(uuid.uuid4())

    def close(self) -> None:
        self.http.close()

    def _extract_access_token(self) -> str:
        for cookie in self.http.cookies.jar:
            value = str(cookie.value or "")
            if value.count(".") == 2 and value.startswith("eyJ"):
                return value
        return ""

    def _log_response(self, response: httpx.Response) -> None:
        self.log.info(
            "%s %s -> %s content=%s query=%s",
            response.request.method,
            safe_url(str(response.request.url)),
            response.status_code,
            response.headers.get("content-type", ""),
            safe_query(str(response.request.url)),
        )

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"value": data}

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self.http.request(method, url, **kwargs)
        self._log_response(response)
        return response

    @staticmethod
    def _find_value(value: Any, keys: set[str]) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in keys and isinstance(item, (str, int)):
                    return str(item)
                found = ChatGPTApiClient._find_value(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = ChatGPTApiClient._find_value(item, keys)
                if found:
                    return found
        return ""

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.sentinel_token:
            headers["openai-sentinel-token"] = self.config.sentinel_token
        headers["x-access-flow-invocation-id"] = self.flow_id
        headers["x-openai-document-navigation-id"] = self.flow_id
        return headers

    def login(self) -> dict[str, Any]:
        self.log.info("login start email=%s", self.config.email[:2] + "***")
        self._request("GET", f"{CHATGPT}/auth/login")
        csrf_response = self._request("GET", f"{CHATGPT}/api/auth/csrf", headers={"Accept": "application/json"})
        csrf = self._json(csrf_response)
        csrf_token = str(csrf.get("csrfToken") or "")
        if not csrf_token:
            raise ApiLoginError("csrf_token_missing")
        self.http.cookies.set("oai-did", self.device_id, domain="chatgpt.com", path="/")

        query = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.flow_id,
            "ext-passkey-client-capabilities": "0111",
            "screen_hint": "login_or_signup",
            "login_hint": self.config.email,
        }
        form = {"callbackUrl": f"{CHATGPT}/", "csrfToken": csrf_token, "json": "true"}
        signin = self._request(
            "POST",
            f"{CHATGPT}/api/auth/signin/openai",
            params=query,
            data=form,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        signin_data = self._json(signin)
        auth_url = str(signin_data.get("url") or signin.headers.get("location") or "")
        if not auth_url:
            raise ApiLoginError("authorization_url_missing")
        auth_page = self._request("GET", auth_url)
        if auth_page.status_code >= 400:
            raise ApiLoginError(f"authorization_page_http_{auth_page.status_code}")

        password = self._request(
            "POST",
            f"{AUTH}/api/accounts/password/verify",
            json={"password": self.config.password},
            headers=self._auth_headers(),
        )
        password_data = self._json(password)
        self.log.info("password verify response shape=%s", shape(password_data))
        if password.status_code >= 400:
            raise ApiLoginError(f"password_verify_http_{password.status_code}")

        factor_id = self._find_value(password_data, {"id", "factor_id", "factorid"})
        factor_type = self._find_value(password_data, {"type", "factor_type", "factortype"}) or "totp"
        if not self.config.totp_secret:
            raise ApiLoginError("totp_secret_required")
        if not factor_id:
            self.log.warning("password response shape=%s; MFA challenge id not found", shape(password_data))
            raise ApiLoginError("mfa_challenge_id_missing")

        challenge = self._request(
            "POST",
            f"{AUTH}/api/accounts/mfa/issue_challenge",
            json={"id": factor_id, "type": factor_type, "force_fresh_challenge": True},
            headers=self._auth_headers(),
        )
        if challenge.status_code >= 400:
            raise ApiLoginError(f"mfa_issue_http_{challenge.status_code}")
        challenge_data = self._json(challenge)
        self.log.info("mfa issue response shape=%s", shape(challenge_data))
        challenge_id = self._find_value(challenge_data, {"id", "challenge_id", "challengeid"}) or factor_id
        code = totp_now(self.config.totp_secret)
        verified = self._request(
            "POST",
            f"{AUTH}/api/accounts/mfa/verify",
            json={"id": challenge_id, "type": factor_type, "code": code},
            headers=self._auth_headers(),
        )
        verified_data = self._json(verified)
        self.log.info("mfa verify response shape=%s", shape(verified_data))
        if verified.status_code >= 400:
            raise ApiLoginError(f"mfa_verify_http_{verified.status_code}")

        callback_url = verified.headers.get("location", "")
        callback_url = callback_url or self._find_value(
            verified_data,
            {"redirect_url", "redirecturl", "callback_url", "callbackurl", "continue_url", "continueurl"},
        )
        if not callback_url:
            callback_url = self._find_value(
                challenge_data,
                {"redirect_url", "redirecturl", "callback_url", "callbackurl", "continue_url", "continueurl"},
            )
        if not callback_url:
            callback_url = self._find_value(
                password_data,
                {"redirect_url", "redirecturl", "callback_url", "callbackurl", "continue_url", "continueurl"},
            )
        if not callback_url:
            raise ApiLoginError("oauth_callback_url_missing")
        callback_host = (urlsplit(callback_url).hostname or "").casefold()
        if callback_host not in {"chatgpt.com", "auth.openai.com"}:
            raise ApiLoginError("oauth_callback_url_untrusted")
        if callback_url:
            callback = self._request("GET", callback_url)
            self.log.info("oauth callback final_url=%s", safe_url(str(callback.url)))
        me = self._request("GET", f"{CHATGPT}/backend-api/me", headers={"Accept": "application/json"})
        if me.status_code >= 400:
            raise ApiLoginError(f"session_me_http_{me.status_code}")
        trial = self._request(
            "GET",
            f"{CHATGPT}/backend-api/accounts/check/v4-2023-04-27",
            params={"timezone_offset_min": "0"},
            headers={"Accept": "application/json"},
        )
        trial_data = self._json(trial)
        self.log.info("login success /backend-api/me HTTP %s", me.status_code)
        return {
            "ok": True,
            "meShape": shape(self._json(me)),
            "callback": bool(callback_url),
            "at": self._extract_access_token(),
            "trialShape": shape(trial_data),
        }

    def change_email(self, new_email: str, endpoint: str, method: str = "POST") -> dict[str, Any]:
        """Call a confirmed email-change endpoint; never guesses or probes destructive routes."""
        if not endpoint:
            raise ApiLoginError("email_change_endpoint_not_confirmed")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "auth.openai.com"}:
            raise ApiLoginError("email_change_endpoint_untrusted")
        self.log.warning("email change requested endpoint=%s new_email=%s***", safe_url(endpoint), new_email[:2])
        response = self._request(method.upper(), endpoint, json={"email": new_email}, headers=self._auth_headers())
        data = self._json(response)
        if response.status_code >= 400:
            raise ApiLoginError(f"email_change_http_{response.status_code}")
        return {"ok": True, "status": response.status_code, "responseShape": shape(data)}
