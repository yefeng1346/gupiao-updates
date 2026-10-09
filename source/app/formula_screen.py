"""TongDaXin formula screener for the local stock tool.

The screener uses the safe expression subset in :mod:`app.formula_engine`. It
reads TongDaXin local daily ``.day`` files or 5-minute ``.lc5`` files, builds
the selected bar period, evaluates the saved formula locally, and verifies
``FINANCE`` predicates with a local CSV or a short Eastmoney financial-data
request. A missing financial value is treated as unknown, never as a pass.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import re
import struct
import time
from typing import Any

from app.config import settings
from app.formula_engine import (
    FormulaSyntaxError,
    evaluate_formula,
    uses_finance,
    validate_formula as validate_saved_formula,
)
from app.providers.akshare_provider import AkShareProvider
from app.providers.tdx_provider import TdxProvider
from app.flow_calendar import latest_closed_trading_day, shanghai_now


DEFAULT_FORMULA = """换手:=VOL/CAPITAL*100;
十日换手:=SUM(换手,10);
VA:=IF(CLOSE>REF(CLOSE,1),VOL,-VOL);
OBV:=SUM(IF(CLOSE=REF(CLOSE,1),0,VA),0);
MAOBV:=MA(OBV,30);
角度:=ATAN((MAOBV/REF(MAOBV,20)-1)*100)*180/3.1416;
OBV角度:=COUNT(MAOBV>REF(MAOBV,1),20)=20 AND 角度>=30;
X_1:=100-100*(HHV(HIGH,55)-CLOSE)/(HHV(HIGH,55)-LLV(LOW,55));
X_2:=100*(HHV(HIGH,13)-CLOSE)/(HHV(HIGH,13)-LLV(LOW,13));
X_3:=MA(X_2,13);
资金:=X_1-X_3;
五日资金:=SUM(资金,5);
X_4:=CROSS(资金,0);
X_5:=REF(X_4,15)>0;
X_6:=EVERY(资金>0,8);
X_7:=(COUNT(X_4,8))>0 AND COUNT(CROSS(五日资金,资金)>0,4) AND X_6;
基本面合格:=FINANCE(25)>0 AND FINANCE(43)>0 AND FINANCE(57)>FINANCE(22) AND FINANCE(9)<50;
选股:OBV角度 AND X_4 AND 基本面合格;"""


_DAY_RECORD = struct.Struct("<5if2i")
# TongDaXin ``.lc5`` stores OHLC and amount as IEEE-754 floats.
_MINUTE_RECORD = struct.Struct("<HHfffffII")
_CODE_RE = re.compile(r"(?i)(?:sh|sz|bj)?(\d{6})\.(?:day|lc5)$")
TIMEFRAME_LABELS: dict[str, str] = {
    "5m": "5分钟",
    "15m": "15分钟",
    "30m": "30分钟",
    "60m": "60分钟",
    "daily": "日线",
    "weekly": "周线",
}
MINUTE_TIMEFRAMES = frozenset({"5m", "15m", "30m", "60m"})
_FINANCE_ALIASES: dict[str, tuple[str, ...]] = {
    "capital": (
        "CAPITAL",
        "FINANCE_7",
        "FINANCE(7)",
        "CIRCULATING_SHARES",
        "CIRCULATING_A_SHARES",
        "FLOAT_SHARES",
        "FREE_SHARES",
        "流通股本",
        "流通A股",
        "流通股",
    ),
    "finance_9": (
        "ZCFZL",
        "DEBT_ASSET_RATIO",
        "资产负债率",
        "资产负债率(%)",
        "DEBTTOASSET",
        "DEBT_TO_ASSET",
    ),
    "finance_22": (
        "ACCOUNTS_RECE",
        "NOTE_ACCOUNTS_RECE",
        "YSZK",
        "应收账款",
        "应收账款合计",
        "ACCOUNTSRECEIVABLE",
        "ACCOUNTS_RECEIVABLE",
    ),
    "finance_25": (
        "NETCASH_OPERATE",
        "NETCASH_OPERATENOTE",
        "经营活动产生的现金流量净额",
        "经营现金流量净额",
        "经营活动现金流量净额",
        "NETCASHOPERATE",
        "NET_CASH_OPERATE",
    ),
    "finance_43": (
        "PARENT_NETPROFIT_YOY",
        "PARENT_NETPROFITTZ",
        "SJLTZ",
        "NETPROFIT_YOY",
        "净利润同比增长率",
        "净利润增长率",
        "净利润同比",
        "NETPROFITYOY",
        "NET_PROFIT_YOY",
    ),
    "finance_57": (
        "MONETARYFUNDS",
        "END_CASH",
        "END_CASH_EQUIVALENTS",
        "货币资金",
        "现金及现金等价物",
        "现金及现金等价物余额",
        "MONETARYFUNDS",
        "CASH_EQUIVALENTS",
    ),
}


def normalize_formula(formula: str) -> str:
    """Normalize copy/paste escaping from Markdown or chat messages."""
    return re.sub(r"\s+", "", str(formula or "").replace("\\_", "_")).upper()


def validate_formula(formula: str) -> list[str]:
    """Validate a saved formula using the safe TongDaXin subset evaluator."""
    return validate_saved_formula(formula)


def _number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "nan", "None", "null"}:
        return None
    text = text.replace("%", "")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(number) else number


def _safe_code(path: Path) -> str:
    match = _CODE_RE.search(path.name)
    return match.group(1) if match else ""


def _stock_identity(path: Path) -> tuple[str, str] | None:
    """Whitelist listed A-share markets, never infer identity from digits alone."""
    match = re.fullmatch(r"(sh|sz|bj)(\d{6})\.(day|lc5)",path.name,re.I)
    if not match:
        return None
    market,code = match[1].lower(),match[2]
    allowed = (market == "sh" and code.startswith(("600","601","603","605","688","689"))
               or market == "sz" and code.startswith(("000","001","002","003","300","301"))
               or market == "bj" and code.startswith(("43","83","87","88","92")))
    return (market,code) if allowed else None


def _unique_stock_files(paths):
    unique = {}
    for path in paths:
        identity = _stock_identity(path)
        if identity and path.is_file():
            unique.setdefault(identity,path)
    return sorted((code,path) for (_,code),path in unique.items())


def _discover_day_files() -> list[tuple[str, Path]]:
    provider = TdxProvider(
        workers=settings.akshare_workers,
        root=settings.tdx_root or None,
        servers=settings.tdx_servers or None,
    )
    paths: list[Path] = []
    for root in provider.roots:
        for relative in (
            Path("vipdoc") / "sh" / "lday",
            Path("vipdoc") / "sz" / "lday",
            Path("vipdoc") / "bj" / "lday",
            Path("T0002") / "hq_cache",
            Path("hq_cache"),
        ):
            folder = root / relative
            if not folder.is_dir():
                continue
            try:
                paths.extend(folder.glob("*.day"))
            except OSError:
                continue

    return _unique_stock_files(paths)


def _discover_minute_files() -> list[tuple[str, Path]]:
    """Find the local 5-minute files used to build intraday periods.

    TongdaXin installations commonly keep these files under ``minline``;
    some versions put them in ``T0002\\hq_cache`` instead.  The standard
    locations are checked first, followed by a bounded recursive fallback.
    """
    provider = TdxProvider(
        workers=settings.akshare_workers,
        root=settings.tdx_root or None,
        servers=settings.tdx_servers or None,
    )
    paths: list[Path] = []
    for root in provider.roots:
        for relative in (
            Path("vipdoc") / "sh" / "minline",
            Path("vipdoc") / "sz" / "minline",
            Path("vipdoc") / "bj" / "minline",
            Path("vipdoc") / "sh" / "fzline",
            Path("vipdoc") / "sz" / "fzline",
            Path("vipdoc") / "bj" / "fzline",
            Path("T0002") / "hq_cache",
            Path("hq_cache"),
        ):
            folder = root / relative
            if not folder.is_dir():
                continue
            try:
                paths.extend(folder.glob("*.lc5"))
            except OSError:
                continue

    if not paths:
        for root in provider.roots:
            if not root.is_dir():
                continue
            try:
                paths.extend(root.rglob("*.lc5"))
            except OSError:
                continue

    return _unique_stock_files(paths)


def _read_daily(path: Path, max_daily_records: int = 1800) -> list[dict[str, float | date]]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - max_daily_records * _DAY_RECORD.size)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return []

    daily: list[dict[str, float | date]] = []
    for offset in range(0, len(raw) - _DAY_RECORD.size + 1, _DAY_RECORD.size):
        try:
            raw_date, open_value, high_value, low_value, close_value, amount, volume, _ = _DAY_RECORD.unpack_from(raw, offset)
            trade_date = datetime.strptime(str(raw_date), "%Y%m%d").date()
        except (ValueError, struct.error):
            continue
        values = [
            _number(open_value),
            _number(high_value),
            _number(low_value),
            _number(close_value),
            _number(amount),
            _number(volume),
        ]
        if any(value is None for value in values) or values[3] is None or values[3] <= 0:
            continue
        daily.append(
            {
                "date": trade_date,
                "open": float(values[0]) / 100,
                "high": float(values[1]) / 100,
                "low": float(values[2]) / 100,
                "close": float(values[3]) / 100,
                "amount": float(values[4]),
                "volume": float(values[5]),
            }
        )

    daily.sort(key=lambda item: item["date"])
    return daily


def _aggregate_weekly(daily: list[dict[str, float | date]]) -> list[dict[str, float | date]]:
    weekly: list[dict[str, float | date]] = []
    current_key: date | None = None
    current: dict[str, float | date] | None = None
    for item in daily:
        trade_date = item["date"]
        if not isinstance(trade_date, date):
            continue
        week_key = trade_date - timedelta(days=trade_date.weekday())
        if week_key != current_key:
            if current is not None:
                weekly.append(current)
            current_key = week_key
            current = {
                "date": trade_date,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "amount": float(item["amount"]),
                "volume": float(item["volume"]),
            }
            continue
        if current is None:
            continue
        current["date"] = trade_date
        current["high"] = max(float(current["high"]), float(item["high"]))
        current["low"] = min(float(current["low"]), float(item["low"]))
        current["close"] = float(item["close"])
        current["amount"] = float(current["amount"]) + float(item["amount"])
        current["volume"] = float(current["volume"]) + float(item["volume"])
    if current is not None:
        weekly.append(current)
    return weekly


def _read_minute(path: Path, max_records: int = 20000) -> list[dict[str, float | datetime]]:
    """Read TongdaXin's 32-byte ``.lc5`` 5-minute records."""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - max_records * _MINUTE_RECORD.size)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return []

    bars: list[dict[str, float | datetime]] = []
    for offset in range(0, len(raw) - _MINUTE_RECORD.size + 1, _MINUTE_RECORD.size):
        try:
            date_code, minute_of_day, open_value, high_value, low_value, close_value, amount, volume, _ = _MINUTE_RECORD.unpack_from(raw, offset)
            year = date_code // 2048 + 2004
            month = (date_code % 2048) // 100
            day = (date_code % 2048) % 100
            hour, minute = divmod(minute_of_day, 60)
            trade_datetime = datetime(year, month, day, hour, minute)
        except (ValueError, struct.error):
            continue
        values = (open_value, high_value, low_value, close_value)
        if any(value <= 0 or not math.isfinite(value) for value in values):
            continue
        bars.append(
            {
                "date": trade_datetime,
                "open": float(values[0]),
                "high": float(values[1]),
                "low": float(values[2]),
                "close": float(values[3]),
                "amount": float(amount),
                "volume": float(volume),
            }
        )
    bars.sort(key=lambda item: item["date"])
    return bars


def _aggregate_intraday(
    bars: list[dict[str, float | datetime]], bucket_minutes: int,
) -> list[dict[str, float | datetime]]:
    if bucket_minutes <= 5:
        return bars
    result: list[dict[str, float | datetime]] = []
    current_key: tuple[date, int, int] | None = None
    current: dict[str, float | datetime] | None = None
    for item in bars:
        trade_datetime = item["date"]
        if not isinstance(trade_datetime, datetime):
            continue
        minutes = trade_datetime.hour * 60 + trade_datetime.minute
        # Anchor each trading session separately so the lunch break does not
        # create an artificial multi-hour candle.
        session_start = 570 if minutes < 720 else 780
        # Local 5-minute bars are close-time labelled (09:35 is the first
        # bar). Subtract one minute so boundary bars such as 09:45 stay in
        # the 09:31-09:45 bucket instead of starting a spurious new bucket.
        slot = max(0, (minutes - session_start - 1) // bucket_minutes)
        key = (trade_datetime.date(), session_start, slot)
        if key != current_key:
            if current is not None:
                result.append(current)
            current_key = key
            current = {
                "date": trade_datetime,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "amount": float(item["amount"]),
                "volume": float(item["volume"]),
            }
            continue
        if current is None:
            continue
        current["date"] = trade_datetime
        current["high"] = max(float(current["high"]), float(item["high"]))
        current["low"] = min(float(current["low"]), float(item["low"]))
        current["close"] = float(item["close"])
        current["amount"] = float(current["amount"]) + float(item["amount"])
        current["volume"] = float(current["volume"]) + float(item["volume"])
    if current is not None:
        result.append(current)
    return result


def _read_period(path: Path, timeframe: str) -> list[dict[str, float | date | datetime]]:
    if timeframe in MINUTE_TIMEFRAMES:
        minute_bars = _read_minute(path)
        bucket_minutes = int(timeframe[:-1])
        return _aggregate_intraday(minute_bars, bucket_minutes)
    daily = _read_daily(path)
    return _aggregate_weekly(daily) if timeframe == "weekly" else daily


def _read_weekly(path: Path, max_daily_records: int = 1800) -> list[dict[str, float | date]]:
    """Backward-compatible helper for callers that explicitly need weekly bars."""
    return _aggregate_weekly(_read_daily(path, max_daily_records))


def _sma(values: list[float | None], period: int, index: int) -> float | None:
    if index < period - 1:
        return None
    window = values[index - period + 1 : index + 1]
    if any(value is None for value in window):
        return None
    return sum(float(value) for value in window) / period


def _rolling_extreme(values: list[float], period: int, index: int, maximum: bool) -> float | None:
    if index < period - 1:
        return None
    window = values[index - period + 1 : index + 1]
    return (max if maximum else min)(window)


def _crosses_zero(values: list[float | None], index: int) -> bool:
    if index < 1 or values[index] is None or values[index - 1] is None:
        return False
    return float(values[index]) > 0 and float(values[index - 1]) <= 0


def _evaluate_technical(weekly: list[dict[str, float | date]]) -> dict[str, Any] | None:
    if len(weekly) < 90:
        return None
    closes = [float(item["close"]) for item in weekly]
    highs = [float(item["high"]) for item in weekly]
    lows = [float(item["low"]) for item in weekly]
    volumes = [float(item["volume"]) for item in weekly]

    va: list[float] = []
    obv: list[float] = []
    for index, volume in enumerate(volumes):
        if index == 0:
            value = 0.0
        elif closes[index] > closes[index - 1]:
            value = volume
        elif closes[index] == closes[index - 1]:
            value = 0.0
        else:
            value = -volume
        va.append(value)
        obv.append((obv[-1] if obv else 0.0) + value)

    ma_obv = [_sma([value for value in obv], 30, index) for index in range(len(obv))]
    angle: list[float | None] = []
    for index, current in enumerate(ma_obv):
        if index < 20 or current is None or ma_obv[index - 20] in (None, 0):
            angle.append(None)
            continue
        angle.append(math.atan((current / float(ma_obv[index - 20]) - 1) * 100) * 180 / 3.1416)

    x1: list[float | None] = []
    x2: list[float | None] = []
    x3: list[float | None] = []
    funds: list[float | None] = []
    for index, close in enumerate(closes):
        high55 = _rolling_extreme(highs, 55, index, True)
        low55 = _rolling_extreme(lows, 55, index, False)
        high13 = _rolling_extreme(highs, 13, index, True)
        low13 = _rolling_extreme(lows, 13, index, False)
        if high55 is None or low55 is None or high55 == low55 or high13 is None or low13 is None or high13 == low13:
            x1.append(None)
            x2.append(None)
            x3.append(None)
            funds.append(None)
            continue
        x1.append(100 - 100 * (high55 - close) / (high55 - low55))
        x2.append(100 * (high13 - close) / (high13 - low13))
        x3.append(_sma(x2, 13, index))
        funds.append(None if x3[-1] is None else float(x1[-1]) - float(x3[-1]))

    last = len(weekly) - 1
    angle_ok = (
        angle[last] is not None
        and float(angle[last]) >= 30
        and last >= 20
        and all(
            ma_obv[index] is not None and ma_obv[index - 1] is not None and float(ma_obv[index]) > float(ma_obv[index - 1])
            for index in range(last - 19, last + 1)
        )
    )
    cross_ok = _crosses_zero(funds, last)
    if not (angle_ok and cross_ok):
        return None
    previous_close = closes[last - 1] if last else None
    weekly_pct = None if previous_close in (None, 0) else (closes[last] / previous_close - 1) * 100
    return {
        "latest_date": weekly[last]["date"].isoformat() if isinstance(weekly[last]["date"], date) else str(weekly[last]["date"]),
        "close": closes[last],
        "weekly_pct_change": weekly_pct,
        "obv_angle": float(angle[last]),
        "funds": float(funds[last]),
        "obv": obv[last],
    }


def _series_last(series: object, *names: str) -> float | None:
    if not isinstance(series, dict):
        return None
    for name in names:
        values = series.get(name.upper())
        if not isinstance(values, list) or not values:
            continue
        value = values[-1]
        if value is not None:
            return float(value)
    return None


def _evaluate_custom_formula(
    bars: list[dict[str, float | date | datetime]],
    formula: str,
    finance: dict[str, float | None] | None = None,
    require_true: bool = False,
) -> dict[str, Any] | None:
    """Evaluate a user formula and expose common result columns for the UI."""
    try:
        evaluation = evaluate_formula(bars, formula, finance)
    except FormulaSyntaxError:
        raise
    final_values = evaluation.get("final") or []
    if not isinstance(final_values, list) or not final_values:
        return None
    final_value = final_values[-1]
    if require_true and (final_value is None or abs(float(final_value)) <= 1e-12):
        return None
    if not require_true and final_value is not None and abs(float(final_value)) <= 1e-12:
        return None
    last = bars[-1]
    close = _number(last.get("close"))
    previous_close = _number(bars[-2].get("close")) if len(bars) > 1 else None
    period_pct = (
        None
        if close is None or previous_close in (None, 0)
        else (float(close) / float(previous_close) - 1) * 100
    )
    return {
        "latest_date": last["date"].isoformat() if isinstance(last.get("date"), date) else str(last.get("date")),
        "close": close,
        "period_pct_change": period_pct,
        "weekly_pct_change": period_pct,
        "obv_angle": _series_last(evaluation.get("series"), "OBV角度", "角度"),
        "funds": _series_last(evaluation.get("series"), "资金", "FUNDS"),
        "obv": _series_last(evaluation.get("series"), "OBV"),
        "_evaluation": evaluation,
    }


def _key(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).upper()


def _field_value(row: dict[str, Any], aliases: tuple[str, ...]) -> float | None:
    normalized = {_key(name): value for name, value in row.items()}
    for alias in aliases:
        result = _number(normalized.get(_key(alias)))
        if result is not None:
            return result
    return None


def _finance_pass(finance: dict[str, float | None]) -> tuple[bool | None, str]:
    required = ("finance_9", "finance_22", "finance_25", "finance_43", "finance_57")
    if any(finance.get(name) is None for name in required):
        return None, "基本面数据不完整，未判定"
    passed = (
        float(finance["finance_25"]) > 0
        and float(finance["finance_43"]) > 0
        and float(finance["finance_57"]) > float(finance["finance_22"])
        and float(finance["finance_9"]) < 50
    )
    return passed, "基本面合格" if passed else "基本面不合格"


def _read_local_finance() -> dict[str, dict[str, float | None]]:
    path = settings.root_dir / "data" / "tdx_finance.csv"
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            result: dict[str, dict[str, float | None]] = {}
            for raw in reader:
                code_text = raw.get("code") or raw.get("代码") or raw.get("股票代码") or ""
                digits = re.sub(r"\D", "", str(code_text))
                if len(digits) != 6:
                    continue
                values: dict[str, float | None] = {}
                for name, aliases in _FINANCE_ALIASES.items():
                    values[name] = _field_value(raw, aliases)
                result[digits] = values
            return result
    except (OSError, UnicodeError, csv.Error):
        return {}


def _finance_report_row(code: str, report_name: str) -> dict[str, Any]:
    market = "SH" if code.startswith(("5", "6")) else "BJ" if code.startswith(("4", "8")) else "SZ"
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": f'(SECUCODE="{code}.{market}")',
        "pageNumber": "1",
        "pageSize": "1",
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": "HSF10",
        "client": "PC",
    }
    response = AkShareProvider._direct_http_get(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=(3, 7),
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        return {}
    return rows[0] if isinstance(rows[0], dict) else {}


def _finance_report_prefixes(main_row: dict[str, Any]) -> list[str]:
    """Prefer the F10 table family matching the company's organization type."""
    org_type = str(main_row.get("ORG_TYPE") or "")
    preferred = (
        "B" if "银行" in org_type
        else "I" if "保险" in org_type
        else "S" if "证券" in org_type or "券商" in org_type
        else "G"
    )
    return list(dict.fromkeys([preferred, "G", "B", "I", "S"]))


def _first_finance_report(code: str, suffix: str, prefixes: list[str]) -> dict[str, Any]:
    for prefix in prefixes:
        try:
            row = _finance_report_row(code, f"RPT_F10_FINANCE_{prefix}{suffix}")
        except Exception:
            continue
        if row:
            return row
    return {}


def _finance_from_api(code: str) -> dict[str, float | None]:
    main_row = _finance_report_row(code, "RPT_F10_FINANCE_MAINFINADATA")
    if not main_row:
        raise RuntimeError("财务接口没有返回最近报告期")
    prefixes = _finance_report_prefixes(main_row)
    rows = [
        main_row,
        _first_finance_report(code, "BALANCE", prefixes),
        _first_finance_report(code, "INCOME", prefixes),
        _first_finance_report(code, "CASHFLOW", prefixes),
    ]
    values = {
        name: next(
            (
                value
                for row in rows
                if row
                for value in [_field_value(row, aliases)]
                if value is not None
            ),
            None,
        )
        for name, aliases in _FINANCE_ALIASES.items()
    }
    return values


def _load_finance(code: str, local: dict[str, dict[str, float | None]]) -> tuple[dict[str, float | None], str]:
    if code in local:
        return local[code], "本地 tdx_finance.csv"
    try:
        return _finance_from_api(code), "东方财富公开财务接口"
    except Exception:
        return {}, "基本面接口不可用"


def _enrich_names(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    try:
        from app.providers.quote_fallback import fetch_resilient_quotes

        quotes = fetch_resilient_quotes(codes)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for row in quotes:
        code = re.sub(r"\D", "", str(row.get("代码") or row.get("symbol") or ""))
        name = str(row.get("名称") or row.get("name") or "").strip()
        if len(code) == 6 and name:
            result[code] = name
    return result


def screen_formula(
    formula: str,
    max_results: int = 100,
    timeframe: str = "weekly",
) -> dict[str, Any]:
    timeframe = str(timeframe or "weekly").strip().lower()
    if timeframe not in TIMEFRAME_LABELS:
        supported = "、".join(TIMEFRAME_LABELS.values())
        raise ValueError(f"不支持的选股周期：{timeframe}；可选周期为：{supported}")
    timeframe_label = TIMEFRAME_LABELS[timeframe]
    formula_text = str(formula or "").strip()
    validation_errors = validate_formula(formula_text)
    if validation_errors:
        raise ValueError("公式无法执行：" + "；".join(validation_errors))
    max_results = max(1, min(int(max_results), 300))
    minute_mode = timeframe in MINUTE_TIMEFRAMES
    files = _discover_minute_files() if minute_mode else _discover_day_files()
    source_label = (
        f"通达信普通版本地5分钟线（TDX_ROOT 下 .lc5，按{timeframe_label}合成）"
        if minute_mode
        else f"通达信普通版本地{timeframe_label}（TDX_ROOT 下 .day）"
    )
    if not files:
        if minute_mode:
            warning = (
                "没有找到通达信分钟线 .lc5 文件；请在 .env 设置 TDX_ROOT 为通达信普通版根目录，"
                "并先让通达信完成个股 5 分钟数据更新。5/15/30/60 分钟选股均基于 .lc5。"
            )
        else:
            warning = (
                "没有找到通达信个股 .day 文件；请在 .env 设置 TDX_ROOT 为通达信普通版根目录，"
                "并确认目录内有 vipdoc\\sh|sz|bj\\lday。"
            )
        return {
            "rows": [],
            "scanned_count": 0,
            "technical_candidates": 0,
            "fundamental_verified": 0,
            "warnings": [warning],
            "data_source": source_label,
            "fundamental_source": "未执行",
            "timeframe": timeframe,
            "timeframe_label": timeframe_label,
        }

    is_default_strategy = normalize_formula(formula_text) == normalize_formula(DEFAULT_FORMULA)
    formula_has_finance = uses_finance(formula_text)
    technical: list[dict[str, Any]] = []
    runtime_errors: set[str] = set()
    scan_status = {"read_failed":0,"insufficient_history":0,"no_match":0,"evaluated":0}
    latest_dates = {}
    expected_date, _ = latest_closed_trading_day(shanghai_now().date())

    def scan(item: tuple[str, Path]):
        code, path = item
        bars = _read_period(path, timeframe)
        if not bars:
            return "read_failed",None,None
        latest = bars[-1]["date"]
        if is_default_strategy and len(bars)<90:
            return "insufficient_history",None,latest
        if is_default_strategy:
            metrics = _evaluate_technical(bars)
        else:
            metrics = _evaluate_custom_formula(
                bars,
                formula_text,
                require_true=False,
            )
            if metrics and not formula_has_finance and metrics["_evaluation"]["final"][-1] is None:
                return "insufficient_history",None,latest
        if not metrics:
            return "no_match",None,latest
        metrics.pop("_evaluation",None)
        identity = _stock_identity(path)
        return "evaluated",{"code": code, "market":identity[0] if identity else "", "symbol":path.stem.upper(),**({"_bars":bars} if formula_has_finance else {}), **metrics},latest

    workers = max(1, min(int(settings.akshare_workers), 8))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(scan, item) for item in files]
        for future in as_completed(futures):
            try:
                status,row,latest = future.result()
                scan_status[status] += 1
                if latest is not None:
                    stamp = latest.date() if isinstance(latest,datetime) else latest
                    key = stamp.isoformat()
                    latest_dates[key] = latest_dates.get(key,0)+1
            except FormulaSyntaxError as exc:
                runtime_errors.add(str(exc))
                scan_status["read_failed"] += 1
                row = None
            except Exception:
                scan_status["read_failed"] += 1
                row = None
            if row:
                technical.append(row)

    technical.sort(key=lambda row: (float(row.get("obv_angle") or -999), str(row.get("code"))), reverse=True)
    names = {}
    local_finance = _read_local_finance()
    final: list[dict[str, Any]] = []
    finance_sources: set[str] = set()
    unknown_count = 0

    def add_final(row: dict[str, Any], finance: dict[str, float | None], source: str, status: str) -> None:
        clean_row = {key: value for key, value in row.items() if not str(key).startswith("_")}
        name = names.get(str(row["code"]), str(row["code"]))
        final.append(
            {
                **clean_row,
                "name": name,
                "security_type": "A股",
                "fundamental_status": status,
                "finance_source": source,
                "finance_9": finance.get("finance_9"),
                "finance_22": finance.get("finance_22"),
                "finance_25": finance.get("finance_25"),
                "finance_43": finance.get("finance_43"),
                "finance_57": finance.get("finance_57"),
            }
        )

    finance_deadline = time.monotonic()+120
    for row in technical:
        if not formula_has_finance:
            add_final(row, {}, "未使用基本面条件", "公式条件通过")
            continue
        if time.monotonic()>=finance_deadline:
            unknown_count += 1
            finance_sources.add("财务查询达到等待上限，未判定")
            continue
        try:
            finance, source = _load_finance(str(row["code"]), local_finance)
        except Exception:
            finance, source = {}, "基本面接口不可用"
        finance_sources.add(source)
        if is_default_strategy:
            passed, status = _finance_pass(finance)
        else:
            try:
                custom = _evaluate_custom_formula(row["_bars"], formula_text, finance, require_true=True)
            except FormulaSyntaxError as exc:
                runtime_errors.add(str(exc))
                custom = None
            if custom is None:
                evaluation = evaluate_formula(row["_bars"],formula_text,finance)
                passed = None if evaluation["final"][-1] is None else False
            else:
                passed = True
            status = "基本面条件通过" if passed else "基本面条件不通过或数据不完整"
        if passed is None:
            unknown_count += 1
        if passed is not True:
            continue
        add_final(row, finance, source, status)

    final.sort(key=lambda row: (float(row.get("obv_angle") or -999), float(row.get("funds") or -999)), reverse=True)
    names = _enrich_names([str(row["code"]) for row in final[:max_results]])
    for row in final[:max_results]: row["name"] = names.get(row["code"],row["code"])
    warnings: list[str] = []
    stale_count = sum(count for day,count in latest_dates.items() if day<expected_date.isoformat())
    if stale_count:
        warnings.append(f"有{stale_count}个文件数据落后于最近收盘交易日{expected_date}；本次按文件实际日期计算，不代表最新行情。请先在通达信更新数据。")
    if len(latest_dates)>1:
        warnings.append("本地文件最新日期不一致，选股结果不是同一交易时点；请核对每行数据日期。")
    if scan_status["insufficient_history"]:
        warnings.append(f"有{scan_status['insufficient_history']}个文件历史不足或指标值不可计算，未计为条件不命中。")
    if scan_status["read_failed"]:
        warnings.append(f"有{scan_status['read_failed']}个文件为空、读取失败或公式执行失败，未计为条件不命中。")
    if unknown_count:
        warnings.append(f"有 {unknown_count} 个候选因财务字段缺失或接口不可用，未计入最终选股结果。")
    if formula_has_finance and not local_finance:
        warnings.append("未发现 data\\tdx_finance.csv；基本面条件使用东方财富公开财务接口，网络不可用时会保守剔除。")
    if runtime_errors:
        warnings.append("部分文件无法执行公式：" + "；".join(sorted(runtime_errors)[:3]))
    definition = (
        f"{timeframe_label}严格执行内置 OBV角度、资金上穿和基本面条件；未知财务数据不算通过。"
        if is_default_strategy
        else f"{timeframe_label}按照保存的自定义公式逐项计算；最终条件取公式中的选股表达式。"
    )
    return {
        "rows": final[:max_results],
        "scanned_count": len(files),
        "scan_status":scan_status,
        "file_latest_dates":latest_dates,
        "expected_closed_date":expected_date.isoformat(),
        "stale_file_count":stale_count,
        "fundamental_unknown_count":unknown_count,
        "security_scope":"沪深北A股（不含指数、ETF、债券、回购）",
        "technical_candidates": len(technical),
        "fundamental_verified": len(final),
        "warnings": warnings,
        "data_source": source_label,
        "fundamental_source": "、".join(sorted(finance_sources)) or "未执行",
        "definition": definition,
        "timeframe": timeframe,
        "timeframe_label": timeframe_label,
        "formula_mode": "内置策略" if is_default_strategy else "自定义公式",
        "uses_finance": formula_has_finance,
    }
