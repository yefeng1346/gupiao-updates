from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from html import unescape
import json
import os
from queue import Empty, Queue
import re
from threading import Thread
import time
from typing import Callable

from app.providers.quote_fallback import fetch_resilient_quotes


_THS_CONCEPT_HISTORY_CACHE: dict[str, str] | None = None
_THS_CONCEPT_HISTORY_CACHE_AT = 0.0


class AkShareProvider:
    """使用 AkShare 获取 A 股板块日线和个股快照。

    AkShare 是开源数据采集层，部分接口依赖上游公开数据页面。
    适合 MVP 和个人使用；生产环境建议替换为有 SLA 的授权数据源。
    """

    name = "akshare"
    code_system = "AkShare板块代码"

    def __init__(self, workers: int = 4):
        self.workers = max(1, workers)
        self._ak = None
        self._ths_concept_history_codes: dict[str, str] | None = None
        self._eastmoney_history_unavailable = False

    def _module(self):
        if self._ak is None:
            try:
                import akshare as ak
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 AkShare，请先运行 start.ps1 或 pip install -r requirements.txt"
                ) from exc
            self._ak = ak
        return self._ak

    @staticmethod
    def _columns(frame, candidates: list[str]) -> str | None:
        for name in candidates:
            if name in frame.columns:
                return name
        return None

    @staticmethod
    def _normalize_board_name(value: object) -> str:
        """Normalize board names before matching different provider catalogs."""
        return re.sub(r"[\s\u3000·・,，（）()_\-—/、]+", "", str(value)).lower()

    @classmethod
    def _build_ths_history_code_map(cls, sectors: list[dict]) -> dict[str, str]:
        """Build a conservative name -> THS code map for history fallback.

        AkShare's Eastmoney concept directory uses BKxxxx codes, while the
        public THS history endpoint uses 30xxxx codes.  The board names are
        the stable common key.  Exact normalized names are preferred; a
        suffix-stripped match is only kept when it is unique.
        """
        exact: dict[str, str] = {}
        simplified: dict[str, str | None] = {}
        for sector in sectors:
            code = str(sector.get("sector_code") or "").strip()
            name = str(sector.get("sector_name") or "").strip()
            if not code or not name:
                continue
            normalized = cls._normalize_board_name(name)
            if normalized and normalized not in exact:
                exact[normalized] = code
            short_name = re.sub(r"(概念|板块|行业|指数)$", "", normalized)
            if short_name:
                if short_name in simplified and simplified[short_name] != code:
                    simplified[short_name] = None
                else:
                    simplified[short_name] = code
        for name, code in simplified.items():
            if code and name not in exact:
                exact[name] = code
        return exact

    def _ths_concept_history_code_map(self) -> dict[str, str]:
        """Load the auxiliary THS concept directory with a short-lived cache."""
        global _THS_CONCEPT_HISTORY_CACHE, _THS_CONCEPT_HISTORY_CACHE_AT
        if self._ths_concept_history_codes is not None:
            return self._ths_concept_history_codes
        cache_ttl = 3600 if _THS_CONCEPT_HISTORY_CACHE else 120
        if (
            _THS_CONCEPT_HISTORY_CACHE is not None
            and time.monotonic() - _THS_CONCEPT_HISTORY_CACHE_AT < cache_ttl
        ):
            self._ths_concept_history_codes = _THS_CONCEPT_HISTORY_CACHE
            return self._ths_concept_history_codes
        try:
            # This map is only an optional history fallback.  Keep it quick so
            # a slow THS catalog cannot hold up the main Eastmoney path.
            sectors = self._fetch_ths_catalog_http("concept", timeout=4, attempts=1)
            self._ths_concept_history_codes = self._build_ths_history_code_map(sectors)
            if self._ths_concept_history_codes:
                _THS_CONCEPT_HISTORY_CACHE = self._ths_concept_history_codes
                _THS_CONCEPT_HISTORY_CACHE_AT = time.monotonic()
        except Exception:
            # This mapping is an optimization/fallback.  The caller still
            # tries the direct Eastmoney and AkShare paths when unavailable.
            self._ths_concept_history_codes = {}
            _THS_CONCEPT_HISTORY_CACHE = {}
            _THS_CONCEPT_HISTORY_CACHE_AT = time.monotonic()
        return self._ths_concept_history_codes

    @staticmethod
    def _is_transport_failure(error: Exception) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "remoteendclosedconnection",
                "connection aborted",
                "connection reset",
                "connection refused",
                "sslerror",
                "timed out",
                "timeout",
            )
        )

    @staticmethod
    def _direct_http_get(url: str, **kwargs):
        """Request public market data with explicit proxy/direct fallback.

        Older builds always disabled ``requests`` environment proxies.  That
        is fast on a clean network, but it also bypasses a user's configured
        proxy/VPN helper and turns every public endpoint into a timeout.  The
        default ``auto`` mode only tries the proxy path when proxy variables
        exist; it falls back to direct mode when the configured proxy is bad.
        """
        import requests

        mode = str(os.getenv("MARKET_HTTP_MODE", "auto")).strip().lower()
        if mode not in {"auto", "env", "direct"}:
            mode = "auto"
        proxy_configured = any(
            os.getenv(name, "").strip()
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            )
        )
        if mode == "env":
            trust_modes = (True,)
        elif mode == "direct":
            trust_modes = (False,)
        else:
            trust_modes = (True, False) if proxy_configured else (False,)

        last_error: Exception | None = None
        for trust_env in trust_modes:
            session = requests.Session()
            session.trust_env = trust_env
            try:
                response = session.get(url, **kwargs)
                # A configured proxy can return a gateway error even though
                # the same public endpoint is reachable directly.  Treat
                # transient 5xx responses like transport failures so auto
                # mode gets a chance to try the other route.
                if mode == "auto" and trust_env and 500 <= response.status_code < 600:
                    status = response.status_code
                    response.close()
                    raise requests.HTTPError(f"代理返回 HTTP {status}")
                return response
            except Exception as exc:
                last_error = exc
            finally:
                session.close()
        raise last_error or RuntimeError("公共行情接口请求失败")

    @staticmethod
    def _parse_ths_catalog_html(text: str, sector_type: str) -> list[dict]:
        """Parse the public Tonghuashun board directory without pandas/akshare.

        The normal AkShare adapter can hang while its HTTPS request is being
        retried.  The directory page itself is a small HTML table and is much
        more reliable to parse directly.  Only links to board detail pages are
        accepted, so article links and table headers cannot become catalog rows.
        """
        path = "thshy" if sector_type == "industry" else "gn"
        pattern = re.compile(
            rf"<a\b[^>]*href\s*=\s*[\"'][^\"']*/{path}/detail/code/([^/\"'?]+)[^\"']*[\"'][^>]*>(.*?)</a>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        result: list[dict] = []
        seen: set[str] = set()
        for code, raw_name in pattern.findall(text):
            name = re.sub(r"<[^>]+>", " ", raw_name)
            name = re.sub(r"\s+", " ", unescape(name)).strip()
            code = str(code).strip()
            if not code or not name or code in seen:
                continue
            seen.add(code)
            result.append({"sector_code": code, "sector_name": name})
        return result

    @classmethod
    def _fetch_ths_catalog_http(
        cls, sector_type: str, timeout: float = 8, attempts: int = 2
    ) -> list[dict]:
        """Fetch a THS board directory with short direct HTTP retries."""
        import requests

        path = "thshy" if sector_type == "industry" else "gn"
        urls = [
            f"http://q.10jqka.com.cn/{path}/",
            f"https://q.10jqka.com.cn/{path}/",
        ]
        headers = {"User-Agent": "Mozilla/5.0"}
        last_error: Exception | None = None
        for url in urls:
            for attempt in range(max(1, attempts)):
                try:
                    response = cls._direct_http_get(url, headers=headers, timeout=timeout)
                    response.raise_for_status()
                    text = response.content.decode("gb18030", errors="replace")
                    result = cls._parse_ths_catalog_html(text, sector_type)
                    if not result:
                        raise RuntimeError(f"页面没有解析出有效板块：{url}")
                    return result
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < max(1, attempts):
                        time.sleep(0.5)
        raise RuntimeError(f"同花顺板块目录直连接口不可用：{last_error}") from last_error

    @staticmethod
    def _fetch_eastmoney_catalog_http(sector_type: str) -> list[dict]:
        """Fetch the Eastmoney board directory as a direct JSON fallback."""
        import requests

        # Eastmoney's clist endpoint uses t:3 for concepts and t:2 for
        # industries.  push2delay is included because some networks route the
        # main push2 host through a slow or unavailable node.
        fs = "m:90+t:3" if sector_type == "concept" else "m:90+t:2"
        page_size = 100
        params = {
            "pn": 1,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": fs,
            "fields": "f12,f14",
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        # The delay node is currently more reliable on some domestic/VPN
        # routes than the main node.  Try it first so a catalog refresh does
        # not spend the first round waiting for a known-bad gateway.
        hosts = (
            "https://push2delay.eastmoney.com",
            "https://push2.eastmoney.com",
            "http://push2delay.eastmoney.com",
        )
        last_error: Exception | None = None
        for host in hosts:
            try:
                result = []
                total = None
                for page in range(1, 21):
                    params["pn"] = page
                    response = AkShareProvider._direct_http_get(
                        f"{host}/api/qt/clist/get",
                        params=params,
                        headers=headers,
                        timeout=8,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    data = payload.get("data") or {}
                    items = data.get("diff") or []
                    if total is None:
                        try:
                            total = int(data.get("total") or 0)
                        except (TypeError, ValueError):
                            total = 0
                    for item in items:
                        code = str(item.get("f12") or "").strip()
                        name = str(item.get("f14") or "").strip()
                        if code and name and not any(
                            row["sector_code"] == code for row in result
                        ):
                            result.append({"sector_code": code, "sector_name": name})
                    if not items or len(items) < page_size or (
                        total and len(result) >= total
                    ):
                        break
                if result:
                    return result
                raise RuntimeError(f"返回数据为空：{host}")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"东方财富板块目录直连接口不可用：{last_error}") from last_error

    @staticmethod
    def _fetch_eastmoney_history_http(
        sector_code: str,
        start_date: date,
        end_date: date,
    ):
        """Fetch one Eastmoney BK board history without AkShare's slow wrapper.

        ``stock_board_concept_hist_em`` uses the same public kline endpoint,
        but its default host can fail TLS in some networks.  The HTTP node is
        intentionally tried first and the response is normalized to the
        columns consumed by ``fetch_sector_history``.
        """
        import pandas as pd
        import requests

        columns = [
            "日期",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
            "振幅",
            "涨跌幅",
            "涨跌额",
            "换手率",
        ]
        params = {
            "secid": f"90.{sector_code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": start_date.strftime("%Y%m%d"),
            "end": end_date.strftime("%Y%m%d"),
            "smplmt": "10000",
            "lmt": "1000000",
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        hosts = (
            "http://91.push2his.eastmoney.com",
            "http://push2his.eastmoney.com",
            "https://91.push2his.eastmoney.com",
            "https://push2his.eastmoney.com",
        )
        last_error: Exception | None = None
        for host in hosts:
            try:
                response = AkShareProvider._direct_http_get(
                    f"{host}/api/qt/stock/kline/get",
                    params=params,
                    headers=headers,
                    timeout=(2, 5),
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                klines = data.get("klines") if isinstance(data, dict) else None
                if not klines:
                    detail = (payload.get("dsc") if isinstance(payload, dict) else None) or "没有返回历史数据"
                    raise RuntimeError(f"{sector_code}: {detail}")
                rows = [str(item).split(",") for item in klines]
                frame = pd.DataFrame(rows, columns=columns)
                for name in [
                    "开盘",
                    "收盘",
                    "最高",
                    "最低",
                    "成交量",
                    "成交额",
                    "振幅",
                    "涨跌幅",
                    "涨跌额",
                    "换手率",
                ]:
                    frame[name] = pd.to_numeric(frame[name], errors="coerce")
                return frame
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"东方财富历史直连接口不可用：{last_error}") from last_error

    def list_sectors(self, sector_type: str) -> list[dict]:
        if sector_type == "industry":
            try:
                return self._fetch_ths_catalog_http(sector_type)
            except Exception as http_error:
                ak = self._module()
                try:
                    frame = self._with_retries(
                        ak.stock_board_industry_name_ths,
                        attempts=1,
                        timeout=30,
                    )
                except Exception as ak_error:
                    raise RuntimeError(
                        f"AkShare 行业板块列表不可用；HTTP：{http_error}；AkShare：{ak_error}"
                    ) from ak_error
            code_col = self._columns(frame, ["code", "板块代码", "行业代码"])
            name_col = self._columns(frame, ["name", "板块名称", "行业名称"])
            if not code_col or not name_col:
                raise RuntimeError(
                    f"AkShare 同花顺行业板块字段发生变化：{list(frame.columns)}"
                )
            result = []
            for _, row in frame.iterrows():
                code = str(row[code_col]).strip()
                name = str(row[name_col]).strip()
                if code and name and code.lower() != "nan":
                    result.append({"sector_code": code, "sector_name": name})
            if not result:
                raise RuntimeError("AkShare 同花顺行业板块没有返回数据")
            return result
        try:
            # Keep the original AkShare/Eastmoney code system when available;
            # THS is the next fallback and supplies a complete independent
            # catalog when Eastmoney is blocked by the local network.
            return self._fetch_eastmoney_catalog_http(sector_type)
        except Exception as em_http_error:
            try:
                return self._fetch_ths_catalog_http(sector_type)
            except Exception as ths_http_error:
                ak = self._module()
                try:
                    frame = self._with_retries(
                        ak.stock_board_concept_name_em,
                        attempts=2,
                        timeout=20,
                    )
                    code_candidates = ["板块代码", "代码", "行业板块代码"]
                    name_candidates = ["板块名称", "名称", "行业板块名称"]
                except Exception as em_error:
                    try:
                        frame = self._with_retries(
                            ak.stock_board_concept_name_ths,
                            attempts=2,
                            timeout=20,
                        )
                        code_candidates = ["code", "板块代码", "概念代码"]
                        name_candidates = ["name", "板块名称", "概念名称"]
                    except Exception as ths_error:
                        raise RuntimeError(
                            "概念板块列表接口均不可用；"
                            f"东财HTTP：{em_http_error}；同花顺HTTP：{ths_http_error}；"
                            f"东财AkShare：{em_error}；同花顺AkShare：{ths_error}"
                        ) from ths_error

        code_col = self._columns(frame, code_candidates)
        name_col = self._columns(frame, name_candidates)
        if not code_col or not name_col:
            raise RuntimeError(f"AkShare 板块列表字段发生变化：{list(frame.columns)}")

        result = []
        for _, row in frame.iterrows():
            code = str(row[code_col]).strip()
            name = str(row[name_col]).strip()
            if code and name and code.lower() != "nan":
                result.append({"sector_code": code, "sector_name": name})
        return result

    def fetch_sector_history(
        self,
        sector_type: str,
        sector_code: str,
        sector_name: str,
        start_date: date,
        end_date: date,
        history_code: str | None = None,
    ) -> list[dict]:
        ak = None
        start = start_date.strftime("%Y%m%d")
        end = end_date.strftime("%Y%m%d")
        if sector_type == "industry":
            try:
                frame = self._fetch_ths_history_http(
                    sector_type, sector_code, start_date, end_date
                )
            except Exception as http_error:
                ak = self._module()
                try:
                    frame = self._with_retries(
                        lambda: ak.stock_board_industry_index_ths(
                            symbol=sector_name,
                            start_date=start,
                            end_date=end,
                        )
                    )
                except Exception as ths_error:
                    raise RuntimeError(
                        f"同花顺行业历史接口不可用；HTTP降级：{http_error}；AkShare：{ths_error}"
                    ) from ths_error
        elif sector_code.upper().startswith("BK"):
            frame = None
            history_errors: list[str] = []
            if history_code and history_code != sector_code:
                try:
                    frame = self._fetch_ths_history_http(
                        sector_type, history_code, start_date, end_date
                    )
                except Exception as ths_error:
                    history_errors.append(f"同花顺映射({history_code})：{ths_error}")
            if frame is None:
                try:
                    frame = self._fetch_eastmoney_history_http(
                        sector_code, start_date, end_date
                    )
                except Exception as http_error:
                    history_errors.append(f"东财HTTP直连：{http_error}")
                    if self._is_transport_failure(http_error):
                        self._eastmoney_history_unavailable = True
            if frame is None:
                if self._eastmoney_history_unavailable and not history_code:
                    raise RuntimeError(
                        "东财历史接口当前网络不可达，且该板块没有同花顺历史代码；"
                        "已跳过重复重试"
                    )
                ak = self._module()
                try:
                    frame = self._with_retries(
                        lambda: ak.stock_board_concept_hist_em(
                            symbol=sector_code,
                            period="daily",
                            start_date=start,
                            end_date=end,
                            adjust="",
                        ),
                        attempts=1,
                        timeout=15,
                    )
                except Exception as em_error:
                    history_errors.append(f"AkShare：{em_error}")
                    raise RuntimeError(
                        "；".join(history_errors) or "概念历史接口不可用"
                    ) from em_error
        else:
            try:
                # 当前同花顺目录返回的 30xxxx/88xxxx 代码可以直接走其
                # HTTP 历史接口，绕开部分环境下 HTTPS 的 EOF/代理错误。
                frame = self._fetch_ths_history_http(
                    sector_type, sector_code, start_date, end_date
                )
            except Exception as http_error:
                ak = self._module()
                try:
                    frame = self._with_retries(
                        lambda: ak.stock_board_concept_index_ths(
                            symbol=sector_name,
                            start_date=start,
                            end_date=end,
                        )
                    )
                except Exception as ths_error:
                    try:
                        frame = self._with_retries(
                            lambda: ak.stock_board_concept_hist_em(
                                symbol=sector_code,
                                period="daily",
                                start_date=start,
                                end_date=end,
                                adjust="",
                            )
                        )
                    except Exception as em_error:
                        raise RuntimeError(
                            f"概念历史接口均不可用；HTTP：{http_error}；同花顺：{ths_error}；东财：{em_error}"
                        ) from em_error

        date_col = self._columns(frame, ["日期", "交易日期"])
        close_col = self._columns(frame, ["收盘", "收盘价"])
        pct_col = self._columns(frame, ["涨跌幅", "涨跌幅(%)"])
        amount_col = self._columns(frame, ["成交额", "成交金额"])
        volume_col = self._columns(frame, ["成交量"])
        if not date_col or not close_col:
            raise RuntimeError(
                f"AkShare {sector_name} 历史行情字段发生变化：{list(frame.columns)}"
            )

        result = []
        previous_close = None
        for _, row in frame.iterrows():
            trade_date = row[date_col]
            if hasattr(trade_date, "strftime"):
                trade_date = trade_date.strftime("%Y-%m-%d")
            else:
                trade_date = str(trade_date)[:10]
            close = row.get(close_col)
            pct_change = row.get(pct_col) if pct_col else None
            if pct_change is None and previous_close is not None:
                try:
                    current_close = float(close)
                    pct_change = (current_close / previous_close - 1) * 100
                except (TypeError, ValueError, ZeroDivisionError):
                    pct_change = None
            try:
                previous_close = float(close)
            except (TypeError, ValueError):
                previous_close = None
            result.append(
                {
                    "trade_date": trade_date,
                    "sector_type": sector_type,
                    "sector_code": sector_code,
                    "sector_name": sector_name,
                    "close": close,
                    "pct_change": pct_change,
                    "amount": row.get(amount_col) if amount_col else None,
                    "volume": row.get(volume_col) if volume_col else None,
                }
            )
        return result

    @staticmethod
    def _fetch_ths_history_http(
        sector_type: str,
        sector_code: str,
        start_date: date,
        end_date: date,
    ):
        """Fetch THS board history through its HTTP line endpoint.

        The AkShare THS adapter currently requests the line endpoint over
        HTTPS. Some local networks/proxies terminate that connection, while
        the same public endpoint remains available over HTTP.
        """
        import pandas as pd
        import requests

        def request_with_retry(url: str, request_headers: dict[str, str]):
            last_error = None
            for attempt in range(2):
                try:
                    response = AkShareProvider._direct_http_get(
                        url,
                        headers=request_headers,
                        timeout=10,
                    )
                    response.raise_for_status()
                    return response
                except Exception as exc:
                    last_error = exc
                    if attempt < 1:
                        time.sleep(0.5)
            raise RuntimeError(f"HTTP行情请求失败：{last_error}") from last_error

        path = "thshy" if sector_type == "industry" else "gn"
        headers = {"User-Agent": "Mozilla/5.0"}
        detail = None
        detail_error = None
        for detail_url in (
            f"http://q.10jqka.com.cn/{path}/detail/code/{sector_code}/",
            f"https://q.10jqka.com.cn/{path}/detail/code/{sector_code}/",
        ):
            try:
                detail = request_with_retry(detail_url, headers)
                break
            except Exception as exc:
                detail_error = exc
        if detail is None:
            raise RuntimeError(f"板块详情页请求失败：{sector_code}；{detail_error}") from detail_error
        match = re.search(
            r'<input[^>]+id=["\']clid["\'][^>]+value=["\']([^"\']+)',
            detail.text,
            flags=re.IGNORECASE,
        )
        if not match:
            match = re.search(
                r'<input[^>]+value=["\']([^"\']+)["\'][^>]+id=["\']clid["\']',
                detail.text,
                flags=re.IGNORECASE,
            )
        if not match:
            raise RuntimeError(f"板块详情页没有返回 clid：{sector_code}")
        inner_code = match.group(1)

        rows = []
        for year in range(start_date.year, end_date.year + 1):
            line_url = f"http://d.10jqka.com.cn/v4/line/bk_{inner_code}/01/{year}.js"
            line_headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://q.10jqka.com.cn/",
            }
            try:
                response = request_with_retry(line_url, line_headers)
            except Exception:
                response = request_with_retry(line_url.replace("http://", "https://"), line_headers)
            text = response.text
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                continue
            payload = json.loads(text[start : end + 1])
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                continue
            for item in str(data).split(";"):
                fields = item.split(",")
                if len(fields) < 7 or not re.match(r"^\d{8}$", fields[0]):
                    continue
                trade_date = f"{fields[0][:4]}-{fields[0][4:6]}-{fields[0][6:8]}"
                if start_date.isoformat() <= trade_date <= end_date.isoformat():
                    rows.append([trade_date, *fields[1:7]])
        if not rows:
            raise RuntimeError(f"同花顺历史接口没有返回有效数据：{sector_code}")
        return pd.DataFrame(
            rows,
            columns=["日期", "收盘", "最高", "最低", "开盘", "成交量", "成交额"],
        )

    @staticmethod
    def _run_with_timeout(fetch: Callable[[], object], timeout: float):
        result: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def run():
            try:
                result.put((True, fetch()))
            except BaseException as exc:  # the caller receives the original failure
                result.put((False, exc))

        worker = Thread(target=run, daemon=True)
        worker.start()
        try:
            succeeded, value = result.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError(f"行情接口超过 {timeout:g} 秒没有响应") from exc
        if succeeded:
            return value
        raise value  # type: ignore[misc]

    @classmethod
    def _with_retries(
        cls,
        fetch: Callable[[], object],
        attempts: int = 3,
        timeout: float = 20,
    ):
        last_error = None
        for attempt in range(attempts):
            try:
                return cls._run_with_timeout(fetch, timeout)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"行情接口连续 {attempts} 次请求失败：{last_error}") from last_error

    def sync_sector_history(
        self,
        sector_type: str,
        calendar_days: int = 120,
        max_sectors: int = 0,
        sector_codes: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        cached_sectors: list[dict] | None = None,
    ) -> dict:
        catalog_source = "live"
        catalog_warning = None
        wanted_for_cache = {
            str(item).strip().lower()
            for item in (sector_codes or [])
            if str(item).strip()
        }
        cache_can_match = bool(cached_sectors) and (
            not wanted_for_cache
            or all(
                any(
                    str(sector.get("sector_code", "")).strip().lower() == wanted
                    or str(sector.get("sector_name", "")).strip().lower() == wanted
                    for sector in cached_sectors
                )
                for wanted in wanted_for_cache
            )
        )
        if cached_sectors and (max_sectors > 0 or wanted_for_cache) and cache_can_match:
            all_sectors = cached_sectors
            catalog_source = "cache"
        else:
            try:
                all_sectors = self.list_sectors(sector_type)
            except Exception as live_error:
                if not cached_sectors:
                    raise
                all_sectors = cached_sectors
                catalog_source = "cache"
                catalog_warning = f"实时板块目录不可用，已使用本地目录缓存：{live_error}"
        catalog = [
            {
                "sector_type": sector_type,
                "sector_code": sector["sector_code"],
                "sector_name": sector["sector_name"],
                "code_system": self.code_system,
                "data_source": self.name,
            }
            for sector in all_sectors
        ]
        sectors = all_sectors
        requested = len(sectors)
        if sector_codes:
            wanted = {str(item).strip().lower() for item in sector_codes if str(item).strip()}
            sectors = [
                sector
                for sector in sectors
                if str(sector["sector_code"]).strip().lower() in wanted
                or str(sector["sector_name"]).strip().lower() in wanted
            ]
            if not sectors:
                raise RuntimeError(
                    "没有在当前板块列表中找到输入的代码/名称，请先检查板块类型或输入内容"
                )
        if max_sectors > 0:
            sectors = sectors[:max_sectors]

        history_codes: dict[str, str] = {}
        if sector_type == "concept" and any(
            str(sector.get("sector_code", "")).upper().startswith("BK")
            for sector in sectors
        ):
            history_codes = self._ths_concept_history_code_map()

        end_date = date.today()
        start_date = end_date - timedelta(days=max(calendar_days, 60))
        rows: list[dict] = []
        errors: list[str] = []
        if catalog_warning:
            errors.append(catalog_warning)
        succeeded = 0
        total = len(sectors)

        def fetch_one(sector: dict):
            history_code = history_codes.get(
                self._normalize_board_name(sector.get("sector_name", ""))
            )
            return self.fetch_sector_history(
                sector_type,
                sector["sector_code"],
                sector["sector_name"],
                start_date,
                end_date,
                history_code=history_code,
            )

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(fetch_one, sector): sector for sector in sectors}
            for index, future in enumerate(as_completed(futures), start=1):
                sector = futures[future]
                try:
                    sector_rows = future.result()
                    rows.extend(sector_rows)
                    succeeded += 1
                except Exception as exc:  # 单个板块失败不应阻断整批同步
                    errors.append(
                        f"{sector['sector_code']} {sector['sector_name']}: {type(exc).__name__}: {exc}"
                    )
                if on_progress:
                    on_progress(index, total)

        return {
            "requested_sectors": requested,
            "selected_sectors": total,
            "succeeded_sectors": succeeded,
            "catalog": catalog,
            "selected_catalog": [
                {
                    "sector_type": sector_type,
                    "sector_code": sector["sector_code"],
                    "sector_name": sector["sector_name"],
                    "code_system": self.code_system,
                    "data_source": self.name,
                }
                for sector in sectors
            ],
            "catalog_source": catalog_source,
            "rows": rows,
            "errors": errors,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    def realtime_quotes(self, symbols: list[str]) -> list[dict]:
        ak = self._module()
        wanted = {item.strip() for item in symbols if item.strip()}
        if not wanted:
            return []
        errors = []
        try:
            return fetch_resilient_quotes(wanted)
        except Exception as direct_error:
            errors.append(f"逐股票备用接口：{direct_error}")
        try:
            frame = self._with_retries(
                ak.stock_zh_a_spot_em,
                attempts=1,
                timeout=10,
            )
            code_col = self._columns(frame, ["代码"])
            if not code_col:
                raise RuntimeError(
                    f"AkShare 个股快照字段发生变化：{list(frame.columns)}"
                )
            frame = frame[frame[code_col].astype(str).isin(wanted)]
            return _dataframe_records(frame)
        except Exception as snapshot_error:
            errors.append(f"全市场快照接口：{snapshot_error}")
            raise RuntimeError("实时行情接口均不可用；" + "；".join(errors)) from snapshot_error


def _dataframe_records(frame) -> list[dict]:
    records = []
    for row in frame.to_dict(orient="records"):
        clean = {}
        for key, value in row.items():
            if hasattr(value, "item"):
                value = value.item()
            if value != value:  # NaN
                value = None
            clean[str(key)] = value
        records.append(clean)
    return records
