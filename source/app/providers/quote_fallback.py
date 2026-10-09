from __future__ import annotations

import re
import os
import time
import threading
from typing import Iterable

import requests


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
_HTTP_SESSIONS = threading.local()


def close_reused_sessions():
    for session in getattr(_HTTP_SESSIONS, "sessions", {}).values():
        session.close()
    _HTTP_SESSIONS.sessions = {}


def _request_get(url: str, **kwargs):
    """Use the same proxy/direct policy as the board-data provider."""
    deadline = kwargs.pop("_deadline", None)
    reuse = kwargs.pop("_reuse_session", False)
    mode = str(os.getenv("MARKET_HTTP_MODE", "auto")).strip().lower()
    if mode not in {"auto", "env", "direct"}:
        mode = "auto"
    # requests can also discover a Windows system proxy (for example one
    # configured by a local proxy client) via urllib's platform settings.
    # Looking only at environment variables silently bypasses that proxy.
    proxies = requests.utils.getproxies()
    proxy_configured = any(proxies.get(name) for name in ("http", "https", "all"))
    if mode == "env":
        trust_modes = (True,)
    elif mode == "direct":
        trust_modes = (False,)
    else:
        trust_modes = (True, False) if proxy_configured else (False,)

    last_error = None
    for trust_env in trust_modes:
        sessions = getattr(_HTTP_SESSIONS, "sessions", {})
        # Proxy configuration is part of the key, so changing Clash settings
        # never reuses a connection created under an obsolete route.
        session_key = (trust_env, tuple(sorted((str(k), str(v)) for k, v in proxies.items())))
        session = sessions.get(session_key) if reuse else None
        if session is None:
            session = requests.Session()
            session.trust_env = trust_env
            if reuse:
                if len(sessions) >= 4:
                    close_reused_sessions()
                    sessions = {}
                sessions[session_key] = session
                _HTTP_SESSIONS.sessions = sessions
        try:
            request_options = dict(kwargs)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout("本次行情查询已达到等待上限")
                timeout = request_options.get("timeout", (3, 5))
                connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
                request_options["timeout"] = (
                    min(float(connect), remaining / 2),
                    min(float(read), remaining / 2),
                )
            response = session.get(url, **request_options)
            if mode == "auto" and trust_env and 500 <= response.status_code < 600:
                status = response.status_code
                response.close()
                raise requests.HTTPError(f"代理返回 HTTP {status}")
            return response
        except Exception as exc:
            last_error = exc
            if reuse:
                session.close()
                sessions.pop(session_key, None)
        finally:
            if not reuse:
                session.close()
    raise last_error or RuntimeError("行情接口请求失败")


def normalize_symbol(value: str) -> str:
    """Normalize common A-share symbol forms to a numeric code."""
    raw = str(value or "").strip().upper().replace("/", ".")
    if "." in raw:
        parts = [part for part in raw.split(".") if part]
        if len(parts) == 2 and parts[0] in {"SH", "SZ", "BJ"}:
            raw = parts[1]
    for prefix in ("SH", "SZ", "BJ"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    if not raw.isdigit():
        raise ValueError(f"无法识别股票代码：{value}")
    return raw


def _secid(code: str) -> str:
    # Eastmoney uses market 1 for Shanghai and market 0 for Shenzhen/Beijing.
    market = "1" if code.startswith(("5", "6")) else "0"
    return f"{market}.{code}"


def _number(value, scale: float = 1.0):
    if value in (None, "", "-"):
        return None
    try:
        number = float(value) / scale
        return None if number != number else number
    except (TypeError, ValueError):
        return None


def _request_json(url: str, params: dict[str, str], attempts: int = 2) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            response = _request_get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("行情接口返回了非 JSON 对象")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"逐股票行情接口请求失败：{last_error}") from last_error


def fetch_eastmoney_quotes(symbols: Iterable[str]) -> list[dict]:
    rows = []
    errors = []
    for raw_symbol in symbols:
        try:
            code = normalize_symbol(raw_symbol)
            payload = _request_json(
                "https://push2.eastmoney.com/api/qt/stock/get",
                {
                    "secid": _secid(code),
                    "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
                },
                attempts=1,
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("接口没有返回该股票的 data")
            rows.append(
                {
                    "代码": str(data.get("f57") or code),
                    "名称": data.get("f58"),
                    "最新价": _number(data.get("f43"), 100),
                    "昨收": _number(data.get("f60"), 100),
                    "今开": _number(data.get("f46"), 100),
                    "最高": _number(data.get("f44"), 100),
                    "最低": _number(data.get("f45"), 100),
                    "涨跌额": _number(data.get("f169"), 100),
                    "涨跌幅": _number(data.get("f170"), 100),
                    "成交量": _number(data.get("f47")),
                    "成交额": _number(data.get("f48")),
                    "数据源": "eastmoney_specific",
                }
            )
        except Exception as exc:
            errors.append(f"{raw_symbol}: {exc}")
    if rows:
        return rows
    raise RuntimeError("；".join(errors) or "逐股票行情接口没有返回数据")


def fetch_tencent_quotes(symbols: Iterable[str]) -> list[dict]:
    normalized = []
    for raw_symbol in symbols:
        code = normalize_symbol(raw_symbol)
        prefix = "sh" if code.startswith(("5", "6")) else "sz"
        normalized.append((code, f"{prefix}{code}"))
    url = "https://qt.gtimg.cn/q=" + ",".join(item[1] for item in normalized)
    response = _request_get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    rows = []
    for match in re.finditer(r'v_(?:sh|sz|bj)(\d+)="([^"]*)"', text):
        fields = match.group(2).split("~")
        if len(fields) < 36:
            continue
        code = match.group(1)
        rows.append(
            {
                "代码": code,
                "名称": fields[1],
                "最新价": _number(fields[3]),
                "昨收": _number(fields[4]),
                "今开": _number(fields[5]),
                "最高": _number(fields[33]),
                "最低": _number(fields[34]),
                "涨跌额": _number(fields[31]),
                "涨跌幅": _number(fields[32]),
                "成交量": _number(fields[6]),
                "成交额": _number(fields[37], 0.0001) if len(fields) > 37 else None,
                "数据源": "tencent_quote",
            }
        )
    if rows:
        return rows
    raise RuntimeError("腾讯逐股票行情接口没有返回数据")


def fetch_resilient_quotes(symbols: Iterable[str]) -> list[dict]:
    symbols = list(symbols)
    errors = []
    # Tencent returns a batch response and is currently the faster path in
    # networks where Eastmoney's per-stock TLS endpoint is intermittent.
    for fetcher in (fetch_tencent_quotes, fetch_eastmoney_quotes):
        try:
            return fetcher(symbols)
        except Exception as exc:
            errors.append(f"{getattr(fetcher, '__name__', 'quote_source')}: {exc}")
    raise RuntimeError("实时行情备用链路均失败；" + "；".join(errors))
