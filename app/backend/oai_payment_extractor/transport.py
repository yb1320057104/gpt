from __future__ import annotations

import os
import base64
from contextvars import ContextVar, Token
import time
import uuid
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:  # pragma: no cover - installation issue handled at runtime
    requests = None  # type: ignore

from .config import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT
from .errors import ConfigurationError, NetworkError, ProtocolError
from .logging_utils import compact_url, emit_log, safe_log_text
from .models import ExtractionConfig

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiSession = None  # type: ignore

try:
    from curl_cffi.requests import RequestException as CurlCffiRequestException  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from curl_cffi.requests.errors import RequestException as CurlCffiRequestException  # type: ignore
    except ImportError:  # pragma: no cover
        CurlCffiRequestException = None  # type: ignore

try:
    from curl_cffi.requests import HTTPError as CurlCffiHTTPError  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from curl_cffi.requests.errors import HTTPError as CurlCffiHTTPError  # type: ignore
    except ImportError:  # pragma: no cover
        CurlCffiHTTPError = None  # type: ignore


class TransportFactory(Protocol):
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any: ...

    def stripe(self, config: ExtractionConfig) -> Any: ...


_REQUEST_TRACE: ContextVar[Any | None] = ContextVar("payment_request_trace", default=None)
_SENSITIVE_FIELD_MARKERS = (
    "authorization", "cookie", "access_token", "client_secret", "confirm_token",
    "confirmation_token", "password", "captcha", "secret", "proxy",
)


def set_request_trace(callback: Any | None) -> Token:
    return _REQUEST_TRACE.set(callback)


def reset_request_trace(token: Token) -> None:
    _REQUEST_TRACE.reset(token)


def _safe_trace_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "[内容层级过深，已省略]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["其余字段"] = f"已省略 {len(value) - 80} 个字段"
                break
            name = str(key)
            if any(marker in name.casefold() for marker in _SENSITIVE_FIELD_MARKERS):
                result[name] = "***已脱敏***"
            else:
                result[name] = _safe_trace_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_trace_value(item, depth=depth + 1) for item in list(value)[:30]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = safe_log_text(value, 500) if isinstance(value, str) else value
        return text
    return safe_log_text(value, 300)


def _trace_response_payload(response: Any) -> Any:
    try:
        payload = response.json()
    except Exception:
        text = safe_log_text(getattr(response, "text", ""), 1000)
        return {"响应文本摘要": text} if text else {"响应体": "空"}
    return _safe_trace_value(payload)


def _emit_request_trace(stage: str, details: dict[str, Any]) -> None:
    callback = _REQUEST_TRACE.get()
    if callback is not None:
        callback("http_request", {"请求阶段": stage, **details})


def new_session() -> Any:
    if CurlCffiSession is not None:
        return CurlCffiSession(impersonate="firefox")
    if requests is None:
        raise ConfigurationError("requests is required; install requirements.txt")
    return requests.Session()


def safe_close(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _is_iprocket_host(host: str) -> bool:
    lowered = str(host or "").lower().rstrip(".")
    return (
        lowered.endswith(".iprocket.io")
        or lowered.endswith(".iprocket.pro")
        or lowered == "proxy.iproyal.net"
        or lowered.endswith(".iproyal.net")
        or lowered == "proxy.iproyal.com"
        or lowered.endswith(".iproyal.com")
        or lowered == "1024proxy.io"
        or lowered.endswith(".1024proxy.io")
    )


def _iprocket_protocol(port: int, scheme: str = "") -> str:
    lowered = str(scheme or "").lower()
    if lowered.startswith("socks"):
        return "socks5"
    if lowered in {"http", "https"}:
        return "http"
    if port in {9595, 59999, 619999}:
        return "socks5"
    if port in {5959, 61999}:
        return "http"
    return "auto"


def chain_bridge_proxy_url(
    host: str,
    port: int,
    username: str,
    password: str,
    scheme: str = "",
) -> str:
    bridge = os.getenv("IPROCKET_CHAIN_PROXY", "http://127.0.0.1:18796")
    protocol = (
        "socks5"
        if "1024proxy." in host.lower()
        else "http" if "iproyal." in host.lower() else _iprocket_protocol(port, scheme)
    )
    metadata = base64.urlsafe_b64encode(
        f"{protocol}|{host}|{port}|{username}".encode("utf-8")
    ).decode("ascii").rstrip("=")
    parsed_bridge = urlsplit(bridge)
    bridge_host = parsed_bridge.hostname or "127.0.0.1"
    bridge_port = parsed_bridge.port or 18796
    return (
        f"http://iprb_{metadata}:{quote(password, safe='')}"
        f"@{bridge_host}:{bridge_port}"
    )


# Backward-compatible internal name used by the existing extractor paths.
_iprocket_bridge_proxy = chain_bridge_proxy_url


def normalize_proxy_url(proxy: str) -> str:
    # The web UI accepts proxy pools (one entry per line).  A transport always
    # receives one proxy, so use the first non-empty entry as a safe fallback
    # for API clients that submit the pool without selecting an entry first.
    lines = [line.strip() for line in str(proxy or "").splitlines() if line.strip()]
    text = lines[0] if lines else ""
    if not text:
        return ""
    # IPRocket share/subscription URL: resolve it to the first exported entry.
    try:
        source = urlsplit(text)
        if (
            source.scheme == "https"
            and source.hostname == "app.iprocket.io"
            and source.path.endswith("/clienta/sysnation/getLink")
        ):
            request = Request(text, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=15) as response:
                exported = [
                    line.strip()
                    for line in response.read(1024 * 1024).decode("utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
            return normalize_proxy_url(exported[0] if exported else "")
    except Exception as exc:
        raise ValueError("IPRocket proxy subscription could not be read") from exc
    # IPRocket QR exports use socks://BASE64 or http://BASE64 rather than a
    # conventional URL. Decode that representation before normalizing.
    if text.lower().startswith(("socks://", "http://")) and "@" not in text:
        encoded = text.split("://", 1)[1].strip()
        try:
            padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
            decoded = base64.b64decode(padded).decode("utf-8").strip()
            if "iprocket." in decoded.lower():
                return normalize_proxy_url(decoded)
        except Exception:
            pass
    had_explicit_scheme = "://" in text
    # IPRocket dashboard export formats 1/2/3. Password remains the fourth
    # field so punctuation inside it is preserved.
    if "://" not in text and "@" not in text:
        separator = next((item for item in (":", "|", ",", ";") if text.count(item) >= 3), ":")
        parts = text.split(separator, 3)
        parsed_vendor: tuple[str, str, str, str] | None = None
        if len(parts) == 4 and _is_iprocket_host(parts[0]) and parts[1].isdigit():  # host:port:user:pass
            parsed_vendor = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 4 and parts[0].isdigit() and _is_iprocket_host(parts[1]):  # port:host:user:pass
            parsed_vendor = parts[1], parts[0], parts[2], parts[3]
        elif len(parts) == 4 and parts[1].isdigit() and _is_iprocket_host(parts[2]):  # pass:port:host:user
            parsed_vendor = parts[2], parts[1], parts[3], parts[0]
        elif len(parts) == 4 and parts[3].isdigit() and _is_iprocket_host(parts[2]):  # user:pass:host:port
            parsed_vendor = parts[2], parts[3], parts[0], parts[1]
        if parsed_vendor is not None:
            host, port, username, password = parsed_vendor
            if _is_iprocket_host(host):
                return _iprocket_bridge_proxy(host, int(port), username, password)
            # Vendor port conventions: IPRocket 9595 and Kookeey gateways are
            # SOCKS5; IPRocket 5959 is HTTP. Resolve DNS through SOCKS as well.
            scheme = (
                "socks5h"
                if port == "9595" or "kookeey" in host.lower()
                else "http"
            )
            text = (
                scheme
                + "://"
                + quote(username, safe="")
                + ":"
                + quote(password, safe="")
                + "@"
                + host
                + ":"
                + port
            )
        elif separator == ":" and len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = parts
            scheme = "socks5h" if "kookeey" in host.lower() else "http"
            text = (
                scheme + "://" + quote(username, safe="") + ":"
                + quote(password, safe="") + "@" + host + ":" + port
            )
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlsplit(text)
    except Exception:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    host = parsed.hostname or ""
    if not host:
        return text
    if _is_iprocket_host(host) and parsed.username is not None:
        try:
            parsed_port = parsed.port or (9595 if parsed.scheme.lower().startswith("socks") else 5959)
        except ValueError as exc:
            raise ValueError("proxy contains an invalid port") from exc
        return _iprocket_bridge_proxy(
            host,
            parsed_port,
            unquote(parsed.username),
            unquote(parsed.password or ""),
            parsed.scheme if had_explicit_scheme else "",
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="%")
        if parsed.password is not None:
            auth += ":" + quote(unquote(parsed.password), safe="%")
        auth += "@"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError as exc:
        raise ValueError("proxy contains an invalid port") from exc
    netloc = auth + host + port
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def set_proxy_url(session: Any, proxy: str) -> None:
    normalized = normalize_proxy_url(proxy)
    session.proxies = {"http": normalized, "https": normalized} if normalized else {}


def stage_http_request(
    session: Any,
    stage: str,
    method: str,
    url: str,
    log: Any | None = None,
    **kwargs: Any,
) -> Any:
    started = time.perf_counter()
    request_fields: dict[str, Any] = {}
    for source_name in ("params", "data", "json"):
        if source_name in kwargs:
            request_fields[source_name] = _safe_trace_value(kwargs[source_name])
    emit_log(log, f"{stage}: {method.upper()} {compact_url(url)}")
    try:
        response = session.request(method.upper(), url, **kwargs)
    except Exception as exc:
        detail = safe_log_text(exc)
        _emit_request_trace(stage, {
            "请求方法": method.upper(), "请求地址": compact_url(url),
            "请求字段": request_fields, "请求结果": "网络异常", "异常信息": detail,
            "耗时毫秒": round((time.perf_counter() - started) * 1000),
        })
        emit_log(log, f"{stage}: request error={detail}")
        # curl_cffi occasionally fails during the TLS handshake for a browser
        # impersonation profile (typically ``invalid library``/``TLS connect
        # error``).  The request never received an HTTP response, so retry it
        # once with a plain curl session while preserving headers, cookies and
        # proxy settings.  This mirrors the known-good UPI extractor fallback
        # and prevents a transient fingerprint failure from exhausting the
        # whole payment task.
        if _is_tls_impersonation_error(exc) and CurlCffiSession is not None:
            fallback = None
            try:
                fallback = CurlCffiSession()
                fallback.trust_env = getattr(session, "trust_env", False)
                if hasattr(session, "verify"):
                    fallback.verify = session.verify
                fallback.headers.update(dict(getattr(session, "headers", {}) or {}))
                fallback.cookies.update(getattr(session, "cookies", {}) or {})
                proxies = getattr(session, "proxies", None)
                if proxies:
                    fallback.proxies.update(dict(proxies))
                emit_log(log, f"{stage}: TLS impersonation failed; retrying plain curl session")
                response = fallback.request(method.upper(), url, **kwargs)
                _emit_request_trace(stage, {
                    "璇锋眰鏂规硶": method.upper(),
                    "璇锋眰鍦板潃": compact_url(url),
                    "fallback": "plain-curl",
                    "鍝嶅簲鐘舵€佺爜": int(response.status_code),
                })
                return response
            except Exception as fallback_exc:
                detail = f"{detail}; plain-curl fallback failed: {safe_log_text(fallback_exc)}"
            finally:
                safe_close(fallback)
        if is_network_exception(exc):
            raise NetworkError(stage, detail) from exc
        raise
    emit_log(
        log,
        f"{stage}: HTTP {response.status_code} elapsed={time.perf_counter() - started:.2f}s",
    )
    _emit_request_trace(stage, {
        "请求方法": method.upper(),
        "请求地址": compact_url(url),
        "请求字段": request_fields,
        "响应状态码": int(response.status_code),
        "响应成功": 200 <= int(response.status_code) < 400,
        "耗时毫秒": round((time.perf_counter() - started) * 1000),
        "响应结果": _trace_response_payload(response),
    })
    return response


def _is_tls_impersonation_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return (
        "tls connect error" in text
        or "openssl_internal:invalid library" in text
        or ("ssl" in text and "impersonat" in text)
    )


def is_network_exception(exc: BaseException) -> bool:
    """Return whether an exception indicates a transport failure.

    HTTP errors are deliberately excluded: an HTTP response means the transport
    completed, even when the provider returned a 4xx or 5xx status.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    if requests is not None:
        request_exceptions = requests.exceptions
        transport_exceptions = (
            request_exceptions.ConnectionError,
            request_exceptions.Timeout,
            request_exceptions.ChunkedEncodingError,
        )
        if isinstance(exc, transport_exceptions):
            return True

    if CurlCffiRequestException is not None:
        if isinstance(exc, CurlCffiRequestException):
            if CurlCffiHTTPError is not None and isinstance(exc, CurlCffiHTTPError):
                return False
            return type(exc).__name__ in {
                "ConnectionError",
                "ConnectTimeout",
                "ProxyError",
                "ReadTimeout",
                "SSLError",
                "Timeout",
            }

    return False


def response_json(response: Any, stage: str) -> dict[str, Any]:
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise ProtocolError(502, f"{stage} invalid json: {safe_log_text(exc)}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(502, f"{stage} returned non-object json")
    return payload


class DefaultTransportFactory:
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any:
        device_id = str(uuid.uuid4())
        session = new_session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": f"{country_locale(config)},en;q=0.9",
                "Authorization": f"Bearer {config.access_token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "oai-device-id": device_id,
                "oai-language": country_locale(config),
                "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        # Keep oai-did in the cookie jar so Checkout response cookies remain
        # attached when the same session switches to the Update proxy.
        session.cookies.set("oai-did", device_id, domain="chatgpt.com")
        set_proxy_url(session, proxy)
        return session

    def stripe(self, config: ExtractionConfig) -> Any:
        session = new_session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": f"{country_locale(config)},en;q=0.9",
            }
        )
        set_proxy_url(session, config.checkout_proxy)
        return session


def country_locale(config: ExtractionConfig) -> str:
    # Config is normalized before a transport is created. Keep this helper
    # dependency-free so fake factories can use the same interface.
    from .config import country_config

    return country_config(config.country)[2]
