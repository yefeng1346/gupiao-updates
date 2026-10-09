from __future__ import annotations

"""Eastmoney sector capital-flow data used by the single-source dashboard."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import math
import json
import re
import random
import threading
import time
from typing import Any

import requests

from app.providers.quote_fallback import _USER_AGENT, _request_get
from app.flow_calendar import confirmed_close_date, latest_closed_trading_day, shanghai_now, trading_window
from app.flow_transport import HISTORY_POOL, FlowHistoryUnavailable, retry_after_seconds
from app.providers.quote_fallback import close_reused_sessions


_EASTMONEY_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
    "http://push2delay.eastmoney.com",
)
_EASTMONEY_DATA_HOSTS = (
    "https://data.eastmoney.com",
    "http://data.eastmoney.com",
)
_EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_FLOW_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f124"
_FLOW_DATA_FIELDS = "f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f2,f3,f124"
_EASTMONEY_HISTORY_HOSTS = (
    "https://push2his.eastmoney.com",
    "http://push2his.eastmoney.com",
)
_FLOW_HISTORY_FIELDS1 = "f1,f2,f3,f7"
_FLOW_HISTORY_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
_FLOW_HISTORY_UT = "b2884a393a59ad64002292a3e90d46a5"
_FLOW_MAX_WORKERS = 2
_FLOW_MAX_ATTEMPTS = 3
_FLOW_QUERY_SECONDS = 60.0
_FLOW_FAILURE_LIMIT = 4
_FLOW_REQUEST_INTERVAL = 0.4
_FLOW_HISTORY_INTERVAL = 1.5
_FLOW_SLOTS = threading.BoundedSemaphore(_FLOW_MAX_WORKERS)
_FLOW_PACE_LOCK = threading.Lock()
_FLOW_NEXT_REQUEST_AT = 0.0


def _paced_flow_get(url: str, *, deadline: float, _interval: float | None = None, **kwargs):
    """Share a small request budget across parallel queries in this process."""
    global _FLOW_NEXT_REQUEST_AT
    if not _FLOW_SLOTS.acquire(timeout=max(0, deadline - time.monotonic())):
        raise requests.Timeout("资金流向查询已达到等待上限")
    try:
        with _FLOW_PACE_LOCK:
            now = time.monotonic()
            request_at = max(now, _FLOW_NEXT_REQUEST_AT)
            _FLOW_NEXT_REQUEST_AT = request_at + (_FLOW_REQUEST_INTERVAL if _interval is None else _interval)
        wait = request_at - time.monotonic()
        if request_at >= deadline:
            raise requests.Timeout("资金流向查询已达到等待上限")
        if wait > 0:
            time.sleep(wait)
        return _request_get(url, _deadline=deadline, **kwargs)
    finally:
        _FLOW_SLOTS.release()


def _number(value: Any, scale: float = 1.0) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
        divisor = float(scale)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not math.isfinite(divisor) or divisor == 0:
        return None
    return number / divisor


def _server_time(value: Any) -> str | None:
    timestamp = _number(value)
    if timestamp is None or timestamp < 1_000_000_000:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def normalize_flow_row(raw: dict[str, Any], rank: int, ratio_scale: float = 1.0) -> dict[str, Any]:
    """Normalize one Eastmoney row.

    The data-center endpoint returns prices and percentages in hundredths,
    while the older ``push2`` endpoint returns them in display units.
    ``ratio_scale`` keeps both transports in the same response shape.
    """

    return {
        "rank": rank,
        "sector_code": str(raw.get("f12") or "").strip(),
        "sector_name": str(raw.get("f14") or "").strip(),
        "latest_price": _number(raw.get("f2"), ratio_scale),
        "pct_change": _number(raw.get("f3"), ratio_scale),
        "main_net_inflow": _number(raw.get("f62")),
        "main_net_ratio": _number(raw.get("f184"), ratio_scale),
        "super_large_net_inflow": _number(raw.get("f66")),
        "super_large_ratio": _number(raw.get("f69"), ratio_scale),
        "large_net_inflow": _number(raw.get("f72")),
        "large_ratio": _number(raw.get("f75"), ratio_scale),
        "medium_net_inflow": _number(raw.get("f78")),
        "medium_ratio": _number(raw.get("f81"), ratio_scale),
        "small_net_inflow": _number(raw.get("f84")),
        "small_ratio": _number(raw.get("f87"), ratio_scale),
        "source_time": _server_time(raw.get("f124")),
    }


def fetch_sector_capital_flow(
    sector_type: str, limit: int = 50, *, deadline: float | None = None
) -> dict[str, Any]:
    """Fetch the current Eastmoney sector capital-flow ranking.

    ``sector_type`` uses the same names as the report page: ``concept`` maps
    to Eastmoney ``t:3`` and ``industry`` maps to ``t:2``.  The endpoint is
    intentionally independent of the selected historical report provider, so
    it can be used in the familiar single-data-source workflow.
    """

    if sector_type not in {"concept", "industry"}:
        raise ValueError("资金流向只支持概念板块或行业板块")
    deadline = deadline or time.monotonic() + 20.0
    safe_limit = max(1, min(int(limit), 1000))
    push_params = {
        "pn": "1",
        "pz": "1000",
        "po": "1",
        "np": "1",
        "ut": _EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fs": "m:90+t:3" if sector_type == "concept" else "m:90+t:2",
        "fields": _FLOW_FIELDS,
    }
    plans = [
        (
            f"{host}/dataapi/bkzj/getbkzj",
            {
                "key": _FLOW_DATA_FIELDS,
                "code": "m:90+t:3" if sector_type == "concept" else "m:90+s:4",
            },
            100.0,
            "东方财富数据中心板块资金流向接口",
        )
        for host in _EASTMONEY_DATA_HOSTS
    ]
    plans.extend(
        (f"{host}/api/qt/clist/get", push_params, 1.0, "东方财富公开板块资金流向接口")
        for host in _EASTMONEY_HOSTS
    )
    last_error: Exception | None = None
    for url, params, ratio_scale, source in plans:
        if time.monotonic() >= deadline:
            break
        response = None
        try:
            response = _paced_flow_get(
                url,
                deadline=deadline,
                params=params,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": "https://data.eastmoney.com/bkzj/gn.html",
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=(3, 5),
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            raw_rows = data.get("diff") if isinstance(data, dict) else None
            if isinstance(raw_rows, dict):
                raw_rows = list(raw_rows.values())
            if not isinstance(raw_rows, list):
                raise RuntimeError("接口没有返回板块资金流向列表")
            rows = [
                normalize_flow_row(row, index, ratio_scale)
                for index, row in enumerate(raw_rows, start=1)
                if isinstance(row, dict) and row.get("f12") and row.get("f14")
            ]
            if not rows:
                raise RuntimeError("接口返回的板块资金流向为空")
            total = int(data.get("total") or len(rows))
            # Do not silently archive a truncated page as the whole universe.
            # The data-center route is unpaged; a truncated response falls back.
            if total > len(rows):
                if "/api/qt/clist/" not in url:
                    raise RuntimeError("完整板块列表未返回，尝试同来源备用通道")
                seen = {row["sector_code"] for row in rows}
                for page in range(2, 21):
                    extra = _paced_flow_get(url, deadline=deadline, params={**params, "pn":str(page)},
                        headers={"User-Agent":_USER_AGENT,"Referer":"https://data.eastmoney.com/"},timeout=(3,5))
                    try:
                        extra.raise_for_status()
                        diff = (extra.json().get("data") or {}).get("diff") or []
                        if isinstance(diff, dict): diff = list(diff.values())
                        addition = [normalize_flow_row(raw, 0, ratio_scale) for raw in diff
                                    if isinstance(raw,dict) and raw.get("f12") and raw.get("f14") and str(raw["f12"]) not in seen]
                        if not addition: break
                        rows.extend(addition); seen.update(row["sector_code"] for row in addition)
                        if len(seen) >= total: break
                    finally:
                        extra.close()
                if len(seen) < total:
                    raise RuntimeError("完整板块列表下载不足，保留原结果")
            rows = list({row["sector_code"]:row for row in rows}.values())
            complete = len(rows) == total
            rows.sort(
                key=lambda row: (
                    row["main_net_inflow"] is not None,
                    row["main_net_inflow"] if row["main_net_inflow"] is not None else float("-inf"),
                ),
                reverse=True,
            )
            selected_rows = rows if safe_limit == 1000 else rows[:safe_limit]
            rows = [dict(row, rank=index) for index, row in enumerate(selected_rows, start=1)]
            source_time = next((row["source_time"] for row in rows if row.get("source_time")), None)
            return {
                "ok": True,
                "sector_type": sector_type,
                "limit": safe_limit,
                "total": total,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "universe_complete": complete and len(rows) == total,
                "updated_at": source_time or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": source,
                "sort": "主力净流入降序",
                "rows": rows,
            }
        except Exception as exc:
            last_error = exc
        finally:
            if response is not None:
                response.close()
    raise RuntimeError(f"东方财富板块资金流向接口不可用：{last_error}") from last_error


def _parse_flow_kline(value: Any, sector_type: str, sector_code: str, sector_name: str) -> dict[str, Any] | None:
    parts = str(value or "").split(",")
    if len(parts) < 13 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0].strip()):
        return None
    try:
        date.fromisoformat(parts[0].strip())
    except ValueError:
        return None
    return {
        "sector_type": sector_type,
        "trade_date": parts[0].strip(),
        "sector_code": sector_code,
        "sector_name": sector_name,
        "main_net_inflow": _number(parts[1]),
        "small_net_inflow": _number(parts[2]),
        "medium_net_inflow": _number(parts[3]),
        "large_net_inflow": _number(parts[4]),
        "super_large_net_inflow": _number(parts[5]),
        "main_net_ratio": _number(parts[6]),
        "small_ratio": _number(parts[7]),
        "medium_ratio": _number(parts[8]),
        "large_ratio": _number(parts[9]),
        "super_large_ratio": _number(parts[10]),
        "latest_price": _number(parts[11]),
        "pct_change": _number(parts[12]),
    }


def _fetch_board_flow_history(
    sector_type: str,
    sector_code: str,
    sector_name: str,
    report_date: str,
    *,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    deadline = min(deadline or float("inf"), time.monotonic() + 24.0)
    params = {
        # This endpoint serves its available history. Do not assume that `end`
        # changes the returned window: filter locally after retrieving history.
        "lmt": "0",
        "klt": "101",
        "fields1": _FLOW_HISTORY_FIELDS1,
        "fields2": _FLOW_HISTORY_FIELDS2,
        "ut": _FLOW_HISTORY_UT,
        "secid": f"90.{sector_code}",
    }
    last_error: Exception | None = None
    tried_channels: set[str] = set()
    retryable = True
    for attempt in range(_FLOW_MAX_ATTEMPTS):
        if time.monotonic() >= deadline:
            break
        channels = HISTORY_POOL.available(tried_channels)
        if not channels:
            last_error = last_error or requests.ConnectionError("资金历史通道正在冷却，稍后自动重试")
            break
        channel_id, host, jsonp = channels[0]
        tried_channels.add(channel_id)
        response = None
        retry_after = 0.0
        try:
            request_params = dict(params)
            if jsonp:
                # The provider's own detail page uses JSONP. This is a format
                # fallback, not a promise that a blocked connection can be fixed.
                request_params["cb"] = "gupiaoFlow"
            response = _paced_flow_get(
                f"{host}/api/qt/stock/fflow/daykline/get",
                deadline=deadline,
                _interval=_FLOW_HISTORY_INTERVAL,
                _reuse_session=True,
                params=request_params,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": f"https://data.eastmoney.com/bkzj/{sector_code}.html",
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=(3, 5),
            )
            if response.status_code in {429, 503}:
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            response.raise_for_status()
            if request_params.get("cb"):
                content = response.text.strip()
                match = re.fullmatch(r"gupiaoFlow\((.*)\);?", content, re.S) if isinstance(content, str) else None
                payload = json.loads(match.group(1)) if match else response.json()
            else:
                payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("code") is not None and str(data["code"]) != sector_code:
                raise ValueError("资金历史接口返回的板块代码不匹配，已拒绝保存")
            raw_rows = data.get("klines") if isinstance(data, dict) else None
            if not isinstance(raw_rows, list):
                raise RuntimeError("历史接口没有返回逐日资金流向")
            rows = [
                row
                for item in raw_rows
                if (row := _parse_flow_kline(item, sector_type, sector_code, sector_name))
                and row["trade_date"] <= report_date
            ]
            if not rows:
                raise RuntimeError("所选日期之前没有可用历史记录，不能把缺失数据视为零流入")
            if not any(row.get("main_net_inflow") is not None for row in rows):
                raise RuntimeError("历史接口未提供有效主力净流入，不能用于确认日期或计算榜单")
            HISTORY_POOL.success(channel_id)
            return sorted(rows, key=lambda row: row["trade_date"])
        except Exception as exc:
            last_error = exc
            close_reused_sessions()
            status = response.status_code if response is not None else 0
            restricted = status == 429 or (status == 503 and retry_after > 0)
            HISTORY_POOL.failure(channel_id, exc, rate_wait=retry_after, rate_limited=restricted)
            if restricted:
                break
            if response is not None and 400 <= status < 500 and status not in {408, 429}:
                retryable = False
                break
        finally:
            if response is not None:
                response.close()
        if attempt + 1 < _FLOW_MAX_ATTEMPTS:
            pause = max(retry_after, 1.5 * (2 ** attempt) + random.uniform(0, 0.3))
            if time.monotonic() + pause >= deadline:
                break
            time.sleep(pause)
    raise FlowHistoryUnavailable(
        f"{sector_name}({sector_code})历史资金流向读取失败：{last_error}",
        retryable=retryable, retry_after=HISTORY_POOL.snapshot()["retry_after_seconds"],
    ) from last_error


def present_sector_capital_flow_report(
    report: dict[str, Any], limit: int, *, cached: bool
) -> dict[str, Any]:
    """Return a display-sized copy while keeping the persisted snapshot complete."""
    safe_limit = max(1, min(int(limit), 1000))
    result = dict(report)
    result["limit"] = safe_limit
    result["rows"] = list(report.get("rows") or [])[:safe_limit]
    result["inflow_days_rank"] = list(report.get("inflow_days_rank") or [])[:safe_limit]
    result["cached"] = cached
    return result


def share_current_flow_with_history(report: dict[str, Any], database: Any) -> dict[str, Any]:
    """Archive only timestamp-confirmed close rows; also bridge pre-upgrade caches."""
    report = dict(report)
    rows = []
    for row in report.get("rows") or []:
        day = confirmed_close_date(row.get("source_time"), captured_at=report.get("captured_at"))
        if day and _number(row.get("main_net_inflow")) is not None:
            rows.append({**row, "sector_type": report["sector_type"], "trade_date": day})
    if rows:
        existing = database.get_sector_capital_flow_daily(
            report["sector_type"], [row["sector_code"] for row in rows], [row["trade_date"] for row in rows])
        by_key = {(row["sector_code"], row["trade_date"]): row for row in existing}
        fields = ("sector_name", "latest_price", "pct_change", "main_net_inflow", "main_net_ratio",
                  "super_large_net_inflow", "super_large_ratio", "large_net_inflow", "large_ratio",
                  "medium_net_inflow", "medium_ratio", "small_net_inflow", "small_ratio")
        changed = [row for row in rows if (row["sector_code"], row["trade_date"]) not in by_key or any(
            row.get(field) is not None and row.get(field) != by_key[(row["sector_code"], row["trade_date"])].get(field)
            for field in fields)]
        database.upsert_sector_capital_flow_daily(changed)
        database.save_sector_capital_flow_catalog(report["sector_type"], report["rows"])
    dates = sorted({row["trade_date"] for row in rows})
    report["confirmed_trade_date"] = dates[-1] if dates else None
    report["closed_rows_saved"] = len(rows)
    report["archive_complete"] = bool(rows and len(rows) == report.get("total") and report.get("universe_complete", True) and len(dates) == 1)
    report["close_status"] = "已归档收盘数据，可供历史单日榜复用" if rows else "盘中数据或日期未确认，仅保存当前快照，不用于10日收盘统计"
    return report


def validate_flow_rule(window_days=10, min_inflow_days=6):
    if (type(window_days) is not int or type(min_inflow_days) is not int
            or not 1 <= window_days <= 60 or not 1 <= min_inflow_days <= window_days):
        raise ValueError("统计天数须为1～60个交易日，至少流入天数须为1～统计天数的整数")
    return window_days, min_inflow_days


def flow_report_with_rule(report, window_days=10, min_inflow_days=6):
    """Keep legacy daily snapshots useful, but never reuse a different screen."""
    validate_flow_rule(window_days, min_inflow_days)
    result = dict(report)
    if (report.get("window_days", 10), report.get("min_inflow_days", 6)) != (window_days, min_inflow_days):
        result.update(inflow_days_rank=[], window_complete=False, window_dates=[], partial=True)
        result["history_coverage"] = {**report.get("history_coverage", {}), "complete_boards": 0,
                                      "window_days": 0, "expected_window_dates": [], "incomplete_samples": []}
        result["warnings"] = [f"旧显示缓存不是{window_days}天内至少{min_inflow_days}天的筛选结果；需逐日档案重新计算，未沿用旧筛选榜。"]
    result.update(window_days=window_days, min_inflow_days=min_inflow_days)
    return result


def fetch_sector_capital_flow_report(
    sector_type: str,
    limit: int,
    requested_date: str | None,
    database: Any,
    *,
    refresh: bool = False,
    retry_missing: bool = False,
    local_only: bool = False,
    daily_only: bool = False,
    window_days: int = 10,
    min_inflow_days: int = 6,
    query_seconds: float = _FLOW_QUERY_SECONDS,
    progress: Any = None,
    cancel: Any = None,
) -> dict[str, Any]:
    """Return a daily ranking and a configurable, complete-trading-day screen."""
    validate_flow_rule(window_days, min_inflow_days)

    try:
        target_day = date.fromisoformat(str(requested_date or shanghai_now().date().isoformat()))
    except ValueError as exc:
        raise ValueError("资金流向日期格式无效") from exc
    if target_day > shanghai_now().date():
        raise ValueError("资金流向日期不能晚于今天")
    if sector_type not in {"concept", "industry"}:
        raise ValueError("资金流向只支持概念板块或行业板块")

    resolved_day, calendar_confirmed = latest_closed_trading_day(target_day)
    cutoff = resolved_day.isoformat()
    deadline = time.monotonic() + max(1, min(query_seconds, 900))
    current = database.get_current_capital_flow(sector_type)
    if current:
        share_current_flow_with_history(current, database)
    # Read history before contacting the provider; existing rows are useful even
    # when no full report snapshot was saved by an older application version.
    cached_rows = database.get_sector_capital_flow_history(sector_type, cutoff, window_days)
    boards = database.get_sector_capital_flow_catalog(sector_type)
    saved = database.get_sector_capital_flow_report(sector_type, cutoff)
    warnings: list[str] = []
    if cutoff != target_day.isoformat():
        reason = "当天尚未收盘" if target_day == shanghai_now().date() and calendar_confirmed else "休市日"
        warnings.append(f"所选 {target_day.isoformat()} 为{reason}，收盘历史截至 {cutoff}；今日盘中数据不参与{window_days}日收盘统计。")
    if not calendar_confirmed:
        warnings.append("该年份的休市日历尚未确认；只展示已取得的实际日期，不把工作日直接当作交易日。")
    catalog_cached = True
    fresh_daily_date = None
    if not local_only and (not boards or (refresh and not retry_missing)):
        try:
            catalog = fetch_sector_capital_flow(sector_type, 1000, deadline=min(deadline, time.monotonic() + 20))
            boards = [row for row in catalog["rows"] if row.get("sector_code") and row.get("sector_name")]
            if not boards:
                raise RuntimeError("东方财富没有返回板块目录")
            database.save_sector_capital_flow_catalog(sector_type, boards)
            catalog = share_current_flow_with_history(catalog, database)
            fresh_daily_date = catalog.get("confirmed_trade_date")
            database.save_current_capital_flow({**catalog, "kind": "current"})
            catalog_cached = False
        except Exception as exc:
            if not boards:
                raise RuntimeError("无法连接东方财富板块目录，且本机没有可用目录；请稍后重试，已保存结果不会删除") from exc
            warnings.append("板块目录联网更新失败，使用本地已保存目录；尚未确认最新板块总数。")
    if not boards:
        raise RuntimeError("本机尚无可用资金流向历史记录")

    boards = list({str(board["sector_code"]): board for board in boards}.values())
    catalog_complete = not any(board.get("catalog_derived") for board in boards)
    if not catalog_complete:
        warnings.append("板块目录仅从本地历史记录恢复，完整范围尚未确认；覆盖数量仅针对已知板块，可联网刷新目录。")
    sector_codes = {str(board["sector_code"]) for board in boards}

    def read_window():
        rows = [row for row in database.get_sector_capital_flow_history(sector_type, cutoff, window_days)
                if str(row["sector_code"]) in sector_codes]
        dates = sorted({str(row["trade_date"]) for row in rows if row.get("main_net_inflow") is not None})[-window_days:]
        expected = trading_window(resolved_day, window_days)
        if expected:
            dates = [day for day in dates if day in expected]
        available: dict[str, set[str]] = {}
        for row in rows:
            if row.get("main_net_inflow") is not None:
                available.setdefault(str(row["sector_code"]), set()).add(str(row["trade_date"]))
        return rows, dates, available

    cached_rows, window_dates, cached_dates = read_window()
    date_confirmed = bool(window_dates and cutoff == window_dates[-1] and calendar_confirmed)
    # On a subsequent retry, try boards not attempted in the previous interrupted
    # run first, rather than getting stuck on the same few failing symbols.
    previous_failures = {row["sector_code"] for row in (saved or {}).get("history_coverage", {}).get("failed_samples", [])}
    boards.sort(key=lambda board: (
        str(board["sector_code"]) in previous_failures if retry_missing else False,
        -len(cached_dates.get(str(board["sector_code"]), set())),
    ))
    failures: list[dict[str, str]] = []
    attempted: set[str] = set()
    downloaded_boards = 0
    consecutive_failures = 0
    stopped_reason = ""
    stop_code = ""
    retry_wait = 0.0

    def needs_download(board):
        code = str(board["sector_code"])
        complete = len(window_dates) == window_days and trading_window(date.fromisoformat(window_dates[-1]), window_days) == window_dates and set(window_dates).issubset(cached_dates.get(code, set()))
        return code not in attempted and (refresh or not date_confirmed or not complete)

    has_target_daily = cutoff in window_dates
    if not local_only and not (daily_only and has_target_daily):
        # One historical worker avoids bursts against the fragile public endpoint;
        # the independently working current endpoint keeps its existing behavior.
        with ThreadPoolExecutor(max_workers=1) as executor:
            while True:
                pending = [board for board in boards if needs_download(board)]
                if not pending:
                    break
                if cancel and cancel.is_set():
                    stopped_reason = "已取消补齐，成功取得的数据已保存。"
                    stop_code = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    stopped_reason = "本次查询达到等待上限，已保存取得的数据；可点击“补齐缺失数据”继续。"
                    stop_code = "budget"
                    break
                if consecutive_failures >= _FLOW_FAILURE_LIMIT:
                    stopped_reason = "历史接口连续连接失败，已暂停请求以避免反复长时间等待；可稍后补齐缺失数据。"
                    stop_code = "transport"
                    break
                batch = pending[:1]
                attempted.update(str(board["sector_code"]) for board in batch)
                futures = {
                    executor.submit(
                        _fetch_board_flow_history, sector_type, str(board["sector_code"]),
                        str(board["sector_name"]), cutoff, deadline=deadline,
                    ): board for board in batch
                }
                batch_successes = 0
                batch_transport_failures = 0
                for future in as_completed(futures):
                    board = futures[future]
                    try:
                        rows = future.result()
                        if rows:
                            # Commit each successful board immediately: later
                            # failures or app shutdown cannot lose earlier data.
                            database.upsert_sector_capital_flow_daily(rows)
                            downloaded_boards += 1
                            batch_successes += 1
                            date_confirmed = True
                    except Exception as exc:
                        retry_wait = max(retry_wait, float(getattr(exc,"retry_after",0)))
                        failures.append({"sector_code": str(board["sector_code"]), "sector_name": str(board["sector_name"]), "error": str(exc),
                                         "retryable": getattr(exc,"retryable",True), "error_type": type(exc.__cause__ or exc).__name__})
                        cause = exc.__cause__ or exc
                        status = getattr(getattr(cause, "response", None), "status_code", 0) or 0
                        if isinstance(cause, (requests.ConnectionError, requests.Timeout)) or (
                            isinstance(cause, requests.HTTPError) and (status >= 500 or status == 429)
                        ):
                            batch_transport_failures += 1
                consecutive_failures = 0 if batch_successes else consecutive_failures + batch_transport_failures
                cached_rows, window_dates, cached_dates = read_window()
                if progress:
                    progress({"attempted": len(attempted), "downloaded": downloaded_boards,
                              "failed": len(failures), "total": len(boards), "cutoff": cutoff,
                              "last_board": str(batch[-1]["sector_name"])})
                if retry_wait > 0:
                    stop_code = "cooldown"
                    stopped_reason = "资金历史通道暂不可用，已保存取得的数据，等待冷却后再重试。"
                    break

    if not window_dates:
        details = failures[-1]["error"] if failures else "尚未取得有效历史数据"
        raise FlowHistoryUnavailable(
            f"历史资金流向连接失败或数据不足，且本机没有可显示记录；不是交易日历错误。{details}",
            retryable=bool(stop_code in {"budget","transport","cooldown"} or any(row.get("retryable") for row in failures)),
            retry_after=retry_wait,
        )
    actual_date = window_dates[-1]
    date_confirmed = calendar_confirmed and actual_date == cutoff
    expected_window = trading_window(resolved_day, window_days)
    window_complete = expected_window is not None and window_dates == expected_window
    if stopped_reason:
        warnings.append(stopped_reason)
    if failures:
        warnings.append(f"本次 {len(failures)} 个板块联网读取失败；已取得的其他板块仍可查看，已有数据继续保留，未读到的数据不计为零。")
    if not date_confirmed:
        warnings.append(f"未能联网确认所选日期的数据，正在显示本地截至 {actual_date} 的历史记录，不能视为已更新到 {cutoff}。")
    if not window_complete:
        warnings.append(f"只有 {len(window_dates)} 个可用交易日：当日榜可查看，{window_days}日资金流入榜暂不能完整计算。")

    rows_by_code: dict[str, dict[str, dict[str, Any]]] = {}
    for row in cached_rows:
        rows_by_code.setdefault(str(row["sector_code"]), {})[str(row["trade_date"])] = row

    daily_rows = [
        row_map[actual_date]
        for row_map in rows_by_code.values()
        if actual_date in row_map and row_map[actual_date].get("main_net_inflow") is not None
    ]
    daily_rows.sort(key=lambda row: float(row["main_net_inflow"]), reverse=True)
    daily_rows = [dict(row, rank=index) for index, row in enumerate(daily_rows, start=1)]

    inflow_rows: list[dict[str, Any]] = []
    complete_count = 0
    for board in boards:
        code = str(board["sector_code"])
        row_map = rows_by_code.get(code, {})
        values = [row_map.get(day) for day in window_dates]
        known = [row for row in values if row is not None and row.get("main_net_inflow") is not None]
        if window_complete and len(known) == window_days:
            complete_count += 1
        positive_days = sum(1 for row in known if float(row["main_net_inflow"]) > 0)
        if window_complete and len(known) == window_days and positive_days >= min_inflow_days:
            inflow_rows.append(
                {
                    "sector_code": code,
                    "sector_name": str(board["sector_name"]),
                    "positive_flow_days": positive_days,
                    "available_days": len(known),
                    # Legacy response alias; new clients use the generic key.
                    "ten_day_main_net_inflow": sum(float(row["main_net_inflow"]) for row in known),
                    "window_main_net_inflow": sum(float(row["main_net_inflow"]) for row in known),
                    "latest_main_net_inflow": float(row_map[actual_date]["main_net_inflow"]),
                    "flow_sequence": [float(row["main_net_inflow"]) for row in values],
                }
            )
    inflow_rows.sort(
        key=lambda row: (
            row["positive_flow_days"],
            row["ten_day_main_net_inflow"],
            row["latest_main_net_inflow"],
        ),
        reverse=True,
    )
    inflow_rows = [dict(row, rank=index) for index, row in enumerate(inflow_rows, start=1)]

    incomplete_boards = len(boards) - complete_count
    if incomplete_boards:
        warnings.append(f"{incomplete_boards} 个板块的{window_days}日数据不完整，已排除出{window_days}日资金流入榜；缺失数据不计为零。")

    report = {
        "ok": True,
        "sector_type": sector_type,
        "requested_date": target_day.isoformat(),
        "resolved_date": cutoff,
        "report_date": actual_date,
        "window_dates": window_dates,
        "window_days": window_days,
        "min_inflow_days": min_inflow_days,
        "total": len(daily_rows),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "东方财富已保存单日资金流向（含当前收盘归档）",
        "sort": "主力净流入降序",
        "rows": daily_rows,
        "inflow_days_rank": inflow_rows,
        "partial": bool(incomplete_boards or not date_confirmed or failures or stopped_reason or not catalog_complete),
        "date_confirmed": date_confirmed,
        "daily_complete": date_confirmed and len(daily_rows) == len(boards) and catalog_complete,
        "window_complete": window_complete,
        "warnings": warnings,
        "catalog_cached": catalog_cached,
        "catalog_complete": catalog_complete,
        "data_saved_at": max((str(row.get("fetched_at") or "") for row in cached_rows), default=""),
        "downloaded_boards": downloaded_boards,
        "stopped_reason": stopped_reason,
        "stop_code": stop_code,
        "retry_recommended": bool(incomplete_boards and (stop_code in {"budget","transport","cooldown"} or any(row.get("retryable") for row in failures))),
        "retry_after_seconds": retry_wait,
        "history_transports": HISTORY_POOL.snapshot() if not local_only else {},
        "refresh_error": "本次联网刷新未取得所查询交易日的新资金流向，继续展示已有记录。" if (refresh or retry_missing) and downloaded_boards == 0 and fresh_daily_date != cutoff and (not date_confirmed or incomplete_boards) else "",
        "history_coverage": {
            "incomplete_samples": [{"sector_code":str(board["sector_code"]),"sector_name":str(board["sector_name"]),
                "available_days":sum(1 for day in (expected_window or []) if rows_by_code.get(str(board["sector_code"]),{}).get(day,{}).get("main_net_inflow") is not None),"expected_days":window_days}
                for board in boards if sum(1 for day in (expected_window or []) if rows_by_code.get(str(board["sector_code"]),{}).get(day,{}).get("main_net_inflow") is not None)<window_days][:10],
            "requested_boards": len(boards),
            "complete_boards": complete_count,
            "failed_boards": len(failures),
            "window_days": len(window_dates),
            "expected_window_dates": expected_window or [],
            "daily_boards": len(daily_rows),
            "incomplete_boards": incomplete_boards,
            "attempted_boards": len(attempted),
            "downloaded_boards": downloaded_boards,
            "unattempted_boards": sum(1 for board in boards if needs_download(board)),
            "failed_samples": failures[:8],
        },
    }
    database.save_sector_capital_flow_report(report)
    return present_sector_capital_flow_report(report, limit, cached=downloaded_boards == 0 and fresh_daily_date != cutoff)
