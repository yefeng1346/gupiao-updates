"""Explicit, source-isolated imports; never derive money flow from OHLCV."""
from __future__ import annotations

import csv
from datetime import date
import io
import json
import math
import re

from app.capital_flow import fetch_sector_capital_flow_report
from app.flow_calendar import is_trading_day, latest_closed_trading_day, shanghai_now

SOURCE_LABELS = {"eastmoney": "东方财富", "tdx_import": "通达信导入", "ths_import": "同花顺导入"}
TEMPLATE = "交易日期,板块代码,板块名称,主力净流入(元),涨跌幅(%),主力净占比(%)\n"
_ALIASES = {
    "trade_date": ("trade_date", "交易日期", "日期", "交易日"),
    "sector_code": ("sector_code", "板块代码", "代码"),
    "sector_name": ("sector_name", "板块名称", "板块", "名称"),
    "main_net_inflow": ("main_net_inflow", "主力净流入", "主力净额", "主力资金净流入", "主力净流入额"),
    "pct_change": ("pct_change", "涨跌幅"),
    "main_net_ratio": ("main_net_ratio", "主力净占比", "主力净流入占比"),
    "super_large_net_inflow": ("super_large_net_inflow", "超大单净流入", "超大单净额"),
    "large_net_inflow": ("large_net_inflow", "大单净流入", "大单净额"),
    "medium_net_inflow": ("medium_net_inflow", "中单净流入", "中单净额"),
    "small_net_inflow": ("small_net_inflow", "小单净流入", "小单净额"),
}
_UNITS = {"元": 1, "万元": 10000, "亿元": 100000000}


def normalize_flow_csv(content: str, sector_type: str, unit: str = "元") -> list[dict]:
    if sector_type not in {"concept", "industry"} or unit not in _UNITS:
        raise ValueError("板块类型或金额单位无效")
    content = content.lstrip("\ufeff")
    first = content.splitlines()[0] if content.strip() else ""
    delimiter = "\t" if "\t" in first else ","
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    fields = reader.fieldnames or []
    mapping = {}
    scales = {}
    for header in fields:
        stripped = header.strip()
        match = re.fullmatch(r"(.+?)[(（](元|万元|亿元|%)[)）]", stripped)
        base = match[1] if match else stripped
        for field, aliases in _ALIASES.items():
            if base in aliases:
                if field in mapping:
                    raise ValueError(f"存在重复字段：{field}")
                mapping[field] = header
                scales[field] = _UNITS.get(match[2], 1) if match else _UNITS[unit]
    required = {"trade_date", "sector_code", "sector_name", "main_net_inflow"}
    if not required.issubset(mapping):
        raise ValueError("文件须包含交易日期、板块代码、板块名称、主力净流入；成交额/普通日线不能代替主力资金。请参考CSV模板。")
    result, seen = [], set()
    latest_close, _ = latest_closed_trading_day(shanghai_now().date())
    for line, item in enumerate(reader, 2):
        if not any(value for value in item.values()):
            continue
        if len(result) >= 50000 or None in item:
            raise ValueError(f"第{line}行格式错误或文件超过50000行")
        row = {field: str(item.get(header) or "").strip() for field, header in mapping.items()}
        stamp = row["trade_date"].replace("/", "-")
        if re.fullmatch(r"\d{8}", stamp):
            stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        try:
            day = date.fromisoformat(stamp)
        except ValueError as exc:
            raise ValueError(f"第{line}行交易日期无效，使用YYYY-MM-DD") from exc
        if is_trading_day(day) is not True or day > latest_close:
            raise ValueError(f"第{line}行不是已确认的收盘交易日；不接受盘中、休市、未来或休市日历尚未确认年份的数据")
        row.update(trade_date=day.isoformat(), sector_type=sector_type)
        row["sector_code"] = row["sector_code"].lstrip("'")
        if not row["sector_code"] or len(row["sector_code"]) > 32 or not row["sector_name"] or len(row["sector_name"]) > 100:
            raise ValueError(f"第{line}行板块代码/名称无效")
        key = (row["trade_date"], row["sector_code"])
        if key in seen:
            raise ValueError(f"第{line}行同日期、同板块重复，未保存任何数据")
        seen.add(key)
        for field in set(mapping) - {"trade_date", "sector_code", "sector_name"}:
            value = row[field].replace(",", "").replace("，", "").replace("%", "")
            if value in {"", "-", "--"}:
                if field == "main_net_inflow":
                    raise ValueError(f"第{line}行主力净流入缺失，不能当作0")
                row[field] = None
                continue
            match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(元|万元|亿元|万|亿)?", value)
            if not match:
                raise ValueError(f"第{line}行{mapping[field]}不是有效数字")
            scale = 1
            if "inflow" in field:
                scale = {**_UNITS, "万": 10000, "亿": 100000000}.get(match[2], scales[field])
            number = float(match[1]) * scale
            if not math.isfinite(number):
                raise ValueError(f"第{line}行数值超出范围")
            row[field] = number
        result.append(row)
    if not result:
        raise ValueError("文件没有数据，模板需先填入真实逐日板块资金数据")
    return result


def import_flow(database, source: str, content: str, sector_type: str, unit="元", overwrite=False):
    if source not in {"tdx_import", "ths_import"}:
        raise ValueError("只能导入通达信或同花顺资金流向；不会覆盖东方财富数据")
    rows = normalize_flow_csv(content, sector_type, unit)
    stamp = shanghai_now().isoformat(timespec="seconds")
    with database.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            key = (source, sector_type, row["trade_date"], row["sector_code"])
            existing = conn.execute("SELECT row_json FROM imported_capital_flow_daily WHERE source=? AND sector_type=? AND trade_date=? AND sector_code=?", key).fetchone()
            if existing and json.loads(existing[0]) != row and not overwrite:
                raise ValueError(f"{row['trade_date']} {row['sector_code']} 已有不同记录；勾选“覆盖同日期同板块记录”后再导入。未保存任何数据。")
            conn.execute("INSERT INTO imported_capital_flow_daily VALUES (?,?,?,?,?,?) ON CONFLICT(source,sector_type,trade_date,sector_code) DO UPDATE SET row_json=excluded.row_json,imported_at=excluded.imported_at",
                         (*key, json.dumps(row, ensure_ascii=False), stamp))
    return {"ok": True, "source_id": source, "rows_saved": len(rows),
            "first_date": min(row["trade_date"] for row in rows), "last_date": max(row["trade_date"] for row in rows)}


class ImportedFlowDatabase:
    """Read-only adapter sharing the existing screen calculation, not its tables."""
    def __init__(self, database, source, cutoff):
        self.database, self.source, self.cutoff = database, source, cutoff

    def get_current_capital_flow(self, sector_type):
        return None

    def get_sector_capital_flow_report(self, *args, **kwargs):
        return None

    def save_sector_capital_flow_report(self, report):
        pass  # All inputs persist; reports rebuild offline without stale snapshots.

    def get_sector_capital_flow_catalog(self, sector_type):
        rows = self.get_sector_capital_flow_history(sector_type, self.cutoff, 1000)
        return list({row["sector_code"]: {"sector_code": row["sector_code"], "sector_name": row["sector_name"], "catalog_derived": True} for row in rows}.values())

    def get_sector_capital_flow_history(self, sector_type, cutoff, window_days=10):
        with self.database.connection() as conn:
            records = conn.execute("""SELECT row_json,imported_at FROM imported_capital_flow_daily
                WHERE source=? AND sector_type=? AND trade_date IN
                (SELECT DISTINCT trade_date FROM imported_capital_flow_daily WHERE source=? AND sector_type=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?)
                ORDER BY trade_date,sector_code""", (self.source, sector_type, self.source, sector_type, cutoff, window_days)).fetchall()
        return [{**json.loads(row[0]), "fetched_at": row[1]} for row in records]


def imported_flow_report(database, source, sector_type, selected_date, limit, *, window_days=10, min_inflow_days=6):
    if source not in {"tdx_import", "ths_import"}:
        raise ValueError("导入数据来源无效")
    if not selected_date:
        with database.connection() as conn:
            latest = conn.execute("SELECT MAX(trade_date) FROM imported_capital_flow_daily WHERE source=? AND sector_type=?", (source, sector_type)).fetchone()[0]
        selected_date = latest
    cutoff = selected_date or shanghai_now().date().isoformat()
    report = fetch_sector_capital_flow_report(sector_type, limit, cutoff, ImportedFlowDatabase(database, source, cutoff), local_only=True,
                                             window_days=window_days, min_inflow_days=min_inflow_days)
    report.update(source_id=source, source=f"{SOURCE_LABELS[source]}（用户声明来源的本地文件）")
    report["warnings"] = [warning for warning in report["warnings"] if "可联网刷新目录" not in warning]
    report["warnings"] = [warning.replace("未能联网确认所选日期的数据", "此来源未导入所选日期的数据") for warning in report["warnings"]]
    coverage = report["history_coverage"]
    report["scope_complete"] = bool(report["date_confirmed"] and report["window_complete"] and
                                    coverage["complete_boards"] == coverage["requested_boards"])
    report["warnings"].insert(0, "仅统计此来源已导入的板块，覆盖数量不代表全市场；缺失日期不会补0，也不会混入东方财富或另一来源。")
    return report
