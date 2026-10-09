from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import os
from typing import Callable

from app.providers.quote_fallback import fetch_resilient_quotes


class EFinanceProvider:
    """使用 efinance 获取板块和个股行情。

    efinance 的板块行情接口同样来自东方财富公开行情接口，
    因此它是 AkShare 的可切换采集层，不是完全不同的数据源。
    """

    name = "efinance"
    code_system = "efinance板块代码"

    def __init__(self, workers: int = 4):
        self.workers = max(1, workers)
        self._ef = None
        # efinance's raw full-market endpoints do not expose a reliable
        # timeout. Keep them disabled in the application path so a provider
        # outage cannot leave a sync request hanging indefinitely. They can
        # still be enabled explicitly for local diagnostics.
        self.allow_slow_fallback = os.getenv("EFINANCE_ALLOW_SLOW_FALLBACK", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _module(self):
        if self._ef is None:
            try:
                import efinance as ef
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 efinance，请先运行 start.ps1 或 pip install -r requirements.txt"
                ) from exc
            self._ef = ef
        return self._ef

    @staticmethod
    def _column(frame, candidates: list[str]) -> str | None:
        for name in candidates:
            if name in frame.columns:
                return name
        return None

    @staticmethod
    def _clean_code(value) -> str:
        code = str(value).strip()
        if code.endswith(".0"):
            code = code[:-2]
        return code

    def _list_sectors_efinance(self, sector_type: str) -> list[dict]:
        ef = self._module()
        market_name = "行业板块" if sector_type == "industry" else "概念板块"
        frame = ef.stock.get_realtime_quotes(market_name)
        code_col = self._column(frame, ["股票代码", "代码"])
        name_col = self._column(frame, ["股票名称", "名称"])
        quote_id_col = self._column(frame, ["行情ID"])
        if not name_col or (not code_col and not quote_id_col):
            raise RuntimeError(f"efinance 板块列表字段发生变化：{list(frame.columns)}")

        result = []
        for _, row in frame.iterrows():
            raw_code = row[code_col] if code_col else str(row[quote_id_col]).split(".")[-1]
            code = self._clean_code(raw_code)
            name = str(row[name_col]).strip()
            if code and name and code.lower() != "nan":
                result.append({"sector_code": code, "sector_name": name})
        if not result:
            raise RuntimeError(f"efinance 没有返回 {market_name} 板块列表")
        return result

    def list_sectors(self, sector_type: str) -> list[dict]:
        try:
            # efinance 的全市场快照接口没有稳定的超时控制；AkShare 的
            # 目录接口有更完整的降级链路，因此兼容模式优先使用它。
            from app.providers.akshare_provider import AkShareProvider

            return AkShareProvider(workers=self.workers).list_sectors(sector_type)
        except Exception as akshare_error:
            if not self.allow_slow_fallback:
                raise RuntimeError(
                    "AkShare directory unavailable; efinance full-market fallback is disabled "
                    "because it has no reliable timeout. Use the local catalog cache or set "
                    "EFINANCE_ALLOW_SLOW_FALLBACK=1 only for diagnostics."
                ) from akshare_error
            try:
                return self._list_sectors_efinance(sector_type)
            except Exception as efinance_error:
                raise RuntimeError(
                    f"兼容模式板块目录失败；AkShare：{akshare_error}；efinance：{efinance_error}"
                ) from efinance_error

    def _fetch_sector_history_efinance(
        self,
        sector_type: str,
        sector_code: str,
        sector_name: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        ef = self._module()
        frame = ef.stock.get_quote_history(
            sector_code,
            beg=start_date.strftime("%Y%m%d"),
            end=end_date.strftime("%Y%m%d"),
            klt=101,
            fqt=0,
            suppress_error=True,
            use_id_cache=True,
        )
        if frame is None or frame.empty:
            raise RuntimeError(f"efinance 没有返回 {sector_name}({sector_code}) 的历史行情")

        date_col = self._column(frame, ["日期", "交易日期"])
        close_col = self._column(frame, ["收盘", "收盘价"])
        pct_col = self._column(frame, ["涨跌幅", "涨跌幅(%)"])
        amount_col = self._column(frame, ["成交额", "成交金额"])
        volume_col = self._column(frame, ["成交量"])
        if not date_col or not close_col:
            raise RuntimeError(f"efinance {sector_name} 历史行情字段发生变化：{list(frame.columns)}")

        result = []
        for _, row in frame.iterrows():
            trade_date = row[date_col]
            if hasattr(trade_date, "strftime"):
                trade_date = trade_date.strftime("%Y-%m-%d")
            else:
                trade_date = str(trade_date)[:10]
            result.append(
                {
                    "trade_date": trade_date,
                    "sector_type": sector_type,
                    "sector_code": sector_code,
                    "sector_name": sector_name,
                    "close": row.get(close_col),
                    "pct_change": row.get(pct_col) if pct_col else None,
                    "amount": row.get(amount_col) if amount_col else None,
                    "volume": row.get(volume_col) if volume_col else None,
                }
            )
        return result

    def fetch_sector_history(
        self,
        sector_type: str,
        sector_code: str,
        sector_name: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        try:
            from app.providers.akshare_provider import AkShareProvider

            return AkShareProvider(workers=1).fetch_sector_history(
                sector_type, sector_code, sector_name, start_date, end_date
            )
        except Exception as akshare_error:
            if not self.allow_slow_fallback:
                raise RuntimeError(
                    "AkShare history unavailable; efinance history fallback is disabled "
                    "because it has no reliable timeout. Use a cached history or set "
                    "EFINANCE_ALLOW_SLOW_FALLBACK=1 only for diagnostics."
                ) from akshare_error
            try:
                return self._fetch_sector_history_efinance(
                    sector_type, sector_code, sector_name, start_date, end_date
                )
            except Exception as efinance_error:
                raise RuntimeError(
                    f"兼容模式历史接口失败；AkShare：{akshare_error}；efinance：{efinance_error}"
                ) from efinance_error

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
                catalog_warning = f"efinance 板块目录不可用，已使用本地目录缓存：{live_error}"
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

        end_date = date.today()
        start_date = end_date - timedelta(days=max(calendar_days, 60))
        rows: list[dict] = []
        errors: list[str] = []
        if catalog_warning:
            errors.append(catalog_warning)
        succeeded = 0
        total = len(sectors)

        def fetch_one(sector: dict):
            return self.fetch_sector_history(
                sector_type,
                sector["sector_code"],
                sector["sector_name"],
                start_date,
                end_date,
            )

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(fetch_one, sector): sector for sector in sectors}
            for index, future in enumerate(as_completed(futures), start=1):
                sector = futures[future]
                try:
                    rows.extend(future.result())
                    succeeded += 1
                except Exception as exc:
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
        wanted = {self._clean_code(item) for item in symbols if item.strip()}
        if not wanted:
            return []
        fallback_error = None
        try:
            return fetch_resilient_quotes(wanted)
        except Exception as exc:
            fallback_error = exc
        if not self.allow_slow_fallback:
            raise RuntimeError(
                "Direct quote fallbacks failed; efinance full-market quote fallback is disabled "
                "because it has no reliable timeout."
            ) from fallback_error
        try:
            ef = self._module()
            frame = ef.stock.get_realtime_quotes()
            code_col = self._column(frame, ["股票代码", "代码"])
            if not code_col:
                raise RuntimeError(
                    f"efinance 个股快照字段发生变化：{list(frame.columns)}"
                )
            frame = frame[frame[code_col].map(self._clean_code).isin(wanted)]
            return _dataframe_records(frame)
        except Exception as efinance_error:
            raise RuntimeError(
                f"兼容模式实时行情失败；备用接口：{fallback_error}；efinance：{efinance_error}"
            ) from efinance_error


def _dataframe_records(frame) -> list[dict]:
    records = []
    for row in frame.to_dict(orient="records"):
        clean = {}
        for key, value in row.items():
            if hasattr(value, "item"):
                value = value.item()
            if value != value:
                value = None
            clean[str(key)] = value
        records.append(clean)
    return records
