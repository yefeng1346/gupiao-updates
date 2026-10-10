"""Offline, dated three-day main-money supplement for the rank-advance table."""
from datetime import date
import math
import re

from .flow_calendar import is_trading_day, shanghai_now, trading_window


def _name(value):
    # Do not remove concept/industry suffixes or perform substring matching.
    return re.sub(r"\s+", "", str(value or "")).casefold()


def attach_three_day_flow(database, sector_type, report_date, module):
    """Keep ranking unchanged; cross-provider matches are labelled references."""
    rows = module.get("rows") or []
    metadata = {"source":"东方财富本地收盘档案", "metric":"主力净流入", "unit":"CNY",
                "report_date":report_date,"window_dates":[],"complete_boards":0,"total_boards":len(rows),
                "definition":"截至报告日期最近3个交易日的主力净流入合计；不是成交额。非东方财富板块仅按唯一同名匹配，成分范围可能不同，作为东方财富参考数据；不足3天不显示部分合计。"}
    module["three_day_flow"] = metadata
    for row in rows:
        row.update(three_day_main_net_inflow=None, three_day_flow_available_days=0,
                   three_day_flow_status="missing", three_day_flow_code=None, three_day_flow_match=None)
    day = date.fromisoformat(report_date)
    now = shanghai_now()
    if day > now.date() or (day == now.date() and (now.hour,now.minute)<(15,5)):
        for row in rows: row["three_day_flow_status"] = "not_closed"
        return
    days = trading_window(day,3) if is_trading_day(day) is True else None
    if not days:
        for row in rows: row["three_day_flow_status"] = "calendar_unknown"
        return
    metadata["window_dates"] = days
    if not rows: return
    catalog = database.get_sector_capital_flow_catalog(sector_type)
    codes = {str(item["sector_code"]).upper():item for item in catalog}
    names = {}
    for code,item in codes.items():
        key = _name(item.get("sector_name"))
        if key: names.setdefault(key,set()).add(code)
    for row in rows:
        code = str(row.get("sector_code") or "").upper()
        if re.fullmatch(r"BK\d{4}",code) and code in codes:
            matched,method = code,"code"
        else:
            candidates = names.get(_name(row.get("sector_name")),set())
            matched,method = (next(iter(candidates)),"unique_name") if len(candidates)==1 else (None,None)
        row.update(three_day_flow_code=matched,three_day_flow_match=method)
        if not matched: row["three_day_flow_status"] = "unmatched"
    matched_codes = [row["three_day_flow_code"] for row in rows if row["three_day_flow_code"]]
    daily = database.get_sector_capital_flow_daily(sector_type,matched_codes,days)
    values = {}
    for item in daily:
        value = item.get("main_net_inflow")
        if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
            values.setdefault(item["sector_code"],{})[item["trade_date"]] = float(value)
    for row in rows:
        if not row["three_day_flow_code"]: continue
        records = values.get(row["three_day_flow_code"],{})
        available = sum(day in records for day in days)
        row["three_day_flow_available_days"] = available
        if available==3:
            total = sum(records[day] for day in days)
            if math.isfinite(total):
                row.update(three_day_main_net_inflow=total,three_day_flow_status="complete")
                metadata["complete_boards"] += 1
