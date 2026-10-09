"""Best-effort board constituent data for the review tables.

The historical board data set contains board-level prices and ranks, not the
individual stocks inside each board. This module supplements the report with
current constituent data and two clearly defined labels:

* 龙头股: constituent with the largest total market value when available;
  turnover is used as a fallback when the public response omits market value.
* 领涨股: constituent with the highest current percentage change.

The lookup is deliberately optional. A board-data or network failure returns
the report rows without leaders instead of blocking the main report.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from threading import Lock
import time
from pathlib import Path
from typing import Any, Iterable

from app.providers.akshare_provider import AkShareProvider
from app.providers.tdx_provider import TdxProvider


_EASTMONEY_BOARD_RE = re.compile(r"^BK\d{4}$", re.IGNORECASE)
_EASTMONEY_CATALOG_CACHE: dict[str, dict[str, str]] = {}
_EASTMONEY_CATALOG_LOCK = Lock()
# The delay node is currently more reliable on some domestic/VPN routes than
# the main node.  Keep it first so a slow main-node failure does not delay a
# click on a board name.
_EASTMONEY_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
    "http://push2delay.eastmoney.com",
)
_CONSTITUENT_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CONSTITUENT_CACHE_LOCK = Lock()
_CONSTITUENT_CACHE_TTL_SECONDS = 30.0
_CONSTITUENT_TIMEOUT = (3, 15)


def _number(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none"} else text


def _stock_code(value: object) -> str:
    text = _text(value)
    if text.endswith(".0"):
        text = text[:-2]
    match = re.search(r"(\d{6})", text)
    if match:
        return match.group(1)
    return text.zfill(6) if text.isdigit() else text


def _board_name_keys(value: object) -> list[str]:
    normalized = re.sub(r"[\s\u3000·・,，（）()_\-—/、]+", "", _text(value)).lower()
    if not normalized:
        return []
    keys = [normalized]
    shortened = re.sub(r"(概念|板块|行业|指数)$", "", normalized)
    if shortened and shortened not in keys:
        keys.append(shortened)
    return keys


def _eastmoney_catalog_map(sector_type: str) -> dict[str, str]:
    """Map normalized board names to Eastmoney BK codes for non-BK sources."""
    with _EASTMONEY_CATALOG_LOCK:
        cached = _EASTMONEY_CATALOG_CACHE.get(sector_type)
        if cached is not None:
            return cached
    sectors = AkShareProvider._fetch_eastmoney_catalog_http(sector_type)
    result: dict[str, str] = {}
    for sector in sectors:
        code = _text(sector.get("sector_code")).upper()
        if not _EASTMONEY_BOARD_RE.fullmatch(code):
            continue
        for key in _board_name_keys(sector.get("sector_name")):
            result.setdefault(key, code)
    with _EASTMONEY_CATALOG_LOCK:
        _EASTMONEY_CATALOG_CACHE[sector_type] = result
    return result


def _board_code(sector: dict[str, Any], catalog_map: dict[str, str]) -> str | None:
    code = _text(sector.get("sector_code")).upper()
    if _EASTMONEY_BOARD_RE.fullmatch(code):
        return code
    for key in _board_name_keys(sector.get("sector_name")):
        mapped = catalog_map.get(key)
        if mapped:
            return mapped
    return None


def _same_board_name(left: object, right: object) -> bool:
    left_keys = set(_board_name_keys(left))
    right_keys = set(_board_name_keys(right))
    if left_keys & right_keys:
        return True
    # Local TDX files occasionally include a suffix such as “行业” or “概念”
    # that is absent from the report catalog.  Only accept a reasonably long
    # containment match to avoid merging unrelated one-character names.
    return any(
        len(candidate) >= 2 and (candidate in target or target in candidate)
        for candidate in left_keys
        for target in right_keys
    )


def _local_tdx_block_paths(sector_type: str) -> list[Path]:
    provider = TdxProvider(
        workers=1,
        root=None,
        servers=None,
    )
    names = (
        ("block_gn.dat", "block.dat", "block_zs.dat")
        if sector_type == "concept"
        else ("block_zs.dat", "block.dat", "block_gn.dat")
    )
    paths: list[Path] = []
    for root in provider.roots:
        for folder in (
            root / "T0002" / "blocknew",
            root / "T0002",
            root / "blocknew",
            root,
        ):
            for name in names:
                paths.append(folder / name)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _fetch_local_tdx_constituents(sector_name: str, sector_type: str) -> list[dict[str, Any]]:
    """Read board membership from the local TDX block files when available."""
    try:
        from pytdx.reader.block_reader import BlockReader, BlockReader_TYPE_GROUP
    except ImportError:
        return []

    for path in _local_tdx_block_paths(sector_type):
        if not path.is_file():
            continue
        try:
            groups = BlockReader().get_data(str(path), BlockReader_TYPE_GROUP)
        except Exception:
            continue
        for group in groups:
            if not isinstance(group, dict) or not _same_board_name(sector_name, group.get("blockname")):
                continue
            raw_codes = str(group.get("code_list") or "").split(",")
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw_code in raw_codes:
                code = _stock_code(raw_code)
                if len(code) != 6 or code.startswith("88") or code in seen:
                    continue
                seen.add(code)
                rows.append(
                    {
                        "code": code,
                        "name": code,
                        "pct_change": None,
                        "market_cap": None,
                        "amount": None,
                        "security_type": _security_type(code, code),
                    }
                )
            if rows:
                return rows
    return []


def _fetch_constituents(board_code: str) -> list[dict[str, Any]]:
    """Fetch current constituent quotes for one Eastmoney BK board."""
    now = time.monotonic()
    with _CONSTITUENT_CACHE_LOCK:
        cached = _CONSTITUENT_CACHE.get(board_code)
        if cached and now - cached[0] <= _CONSTITUENT_CACHE_TTL_SECONDS:
            return [dict(row) for row in cached[1]]

    params = {
        "pn": 1,
        "pz": 100,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        # Do not add a security-type filter here.  The board constituents view
        # is expected to include both ordinary shares and ETFs when the public
        # board endpoint exposes them.
        "fs": f"b:{board_code}",
        "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f100,f102,f103",
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    last_error: Exception | None = None
    for host in _EASTMONEY_HOSTS:
        try:
            rows = []
            seen: set[str] = set()
            total: int | None = None
            for page in range(1, 21):
                page_params = dict(params)
                page_params["pn"] = page
                response = AkShareProvider._direct_http_get(
                    f"{host}/api/qt/clist/get",
                    params=page_params,
                    headers=headers,
                    timeout=_CONSTITUENT_TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and data.get("total") is not None:
                    try:
                        total = int(data["total"])
                    except (TypeError, ValueError):
                        total = None
                diff = data.get("diff") if isinstance(data, dict) else None
                if isinstance(diff, dict):
                    raw_rows = list(diff.values())
                elif isinstance(diff, list):
                    raw_rows = diff
                else:
                    raw_rows = []
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        continue
                    code = _stock_code(raw.get("f12"))
                    name = _text(raw.get("f14"))
                    if not code or not name or code in seen:
                        continue
                    seen.add(code)
                    rows.append(
                        {
                            "code": code,
                            "name": name,
                            "pct_change": _number(raw.get("f3")),
                            "market_cap": _number(raw.get("f20")),
                            "amount": _number(raw.get("f6")),
                            "security_type": _security_type(code, name),
                        }
                    )
                if not raw_rows or (total is not None and len(rows) >= total) or len(raw_rows) < int(params["pz"]):
                    break
            if rows:
                with _CONSTITUENT_CACHE_LOCK:
                    _CONSTITUENT_CACHE[board_code] = (time.monotonic(), [dict(row) for row in rows])
                return rows
            raise RuntimeError(f"板块 {board_code} 没有返回成分股")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"板块成分股接口不可用：{last_error}") from last_error


def _security_type(code: str, name: str) -> str:
    """Classify the public constituent row without pretending it is perfect."""
    upper_name = str(name or "").upper()
    if "ETF" in upper_name or str(code).startswith(("15", "16", "50", "51", "56", "58")):
        return "ETF"
    return "股票"


def fetch_sector_constituents(
    sector_code: str,
    sector_name: str,
    sector_type: str,
    provider_name: str,
) -> dict[str, Any]:
    """Return all currently exposed constituents for one report board.

    Report rows can use several code systems (BKxxxx, THS codes, or imported
    snapshots).  Names are therefore mapped through the Eastmoney directory
    when the row is not already a BK code.  The public response is kept as-is
    apart from stable field names and a conservative stock/ETF label.
    """
    item = {
        "sector_code": _text(sector_code),
        "sector_name": _text(sector_name) or _text(sector_code),
    }
    if not item["sector_code"] and not item["sector_name"]:
        raise ValueError("请先选择一个板块")

    # The ordinary TDX source has the complete membership relation locally.
    # Prefer it so the new click-to-expand view still works when public web
    # endpoints are blocked or the user intentionally works offline.
    if provider_name in {"tdx", "tdx_online", "tdx_standard"}:
        local_rows = _fetch_local_tdx_constituents(item["sector_name"], sector_type)
        if local_rows:
            return {
                **item,
                "provider": provider_name,
                "source": "通达信本地板块文件",
                "rows": local_rows,
                "count": len(local_rows),
                "stock_count": sum(1 for row in local_rows if row.get("security_type") != "ETF"),
                "etf_count": sum(1 for row in local_rows if row.get("security_type") == "ETF"),
                "warnings": ["本地板块文件只保存成员代码；当前网络不可用时名称、涨跌幅、成交额和总市值可能为空。"],
                "definition": "成员来自通达信本地板块文件；证券类型按代码启发式标记。",
            }

    catalog_map: dict[str, str] = {}
    mapping_warning = None
    board_code = _board_code(item, catalog_map)
    if not board_code:
        try:
            catalog_map = _eastmoney_catalog_map(sector_type)
            board_code = _board_code(item, catalog_map)
        except Exception as exc:
            mapping_warning = f"板块代码映射失败：{type(exc).__name__}: {exc}"
    if not board_code:
        raise RuntimeError(
            mapping_warning
            or "当前板块代码或名称无法映射到东方财富公开成分股接口"
        )

    rows = _fetch_constituents(board_code)
    rows.sort(
        key=lambda row: (
            -float(row.get("market_cap") or 0),
            -float(row.get("amount") or 0),
            str(row.get("code") or ""),
        )
    )
    etf_count = sum(1 for row in rows if row.get("security_type") == "ETF")
    return {
        "sector_code": item["sector_code"],
        "sector_name": item["sector_name"],
        "eastmoney_board_code": board_code,
        "provider": provider_name,
        "source": "东方财富公开板块成分股行情",
        "rows": rows,
        "count": len(rows),
        "stock_count": len(rows) - etf_count,
        "etf_count": etf_count,
        "warnings": [mapping_warning] if mapping_warning else [],
        "definition": "成分股列表来自东方财富公开板块接口；类型按名称/代码启发式标记，最终以交易所或券商资料为准。",
    }


def _pick_leaders(rows: list[dict[str, Any]]) -> dict[str, Any]:
    market_cap_available = any(row.get("market_cap") is not None for row in rows)
    amount_available = any(row.get("amount") is not None for row in rows)
    if market_cap_available:
        leader = max(rows, key=lambda row: row.get("market_cap") or float("-inf"))
        leader_basis = "总市值最高"
    elif amount_available:
        leader = max(rows, key=lambda row: row.get("amount") or float("-inf"))
        leader_basis = "成交额最高（总市值缺失时回退）"
    else:
        leader = rows[0]
        leader_basis = "公开行情首条（指标缺失时回退）"

    pct_available = any(row.get("pct_change") is not None for row in rows)
    if pct_available:
        leading = max(rows, key=lambda row: row.get("pct_change") or float("-inf"))
    else:
        leading = rows[0]
    return {
        "leader_stock": leader["name"],
        "leader_stock_code": leader["code"],
        "leader_stock_basis": leader_basis,
        "leading_stock": leading["name"],
        "leading_stock_code": leading["code"],
        "leading_stock_pct_change": leading.get("pct_change"),
    }


def fetch_sector_leaders(
    sectors: Iterable[dict[str, Any]],
    sector_type: str,
    provider_name: str,
) -> dict[str, Any]:
    """Return best-effort leader/leading-stock details for up to 20 boards."""
    unique: dict[str, dict[str, Any]] = {}
    for raw in sectors:
        code = _text(raw.get("sector_code"))
        name = _text(raw.get("sector_name"))
        if code and name and code not in unique:
            unique[code] = {"sector_code": code, "sector_name": name}
    items = list(unique.values())[:20]
    if not items:
        return {
            "leaders": [],
            "warnings": [],
            "source": "东方财富公开板块成分股行情",
            "definition": "龙头股按总市值优先，领涨股按最新涨跌幅；公开字段缺失时会显示回退口径。",
        }

    catalog_map: dict[str, str] = {}
    if any(not _EASTMONEY_BOARD_RE.fullmatch(_text(item["sector_code"]).upper()) for item in items):
        try:
            catalog_map = _eastmoney_catalog_map(sector_type)
        except Exception as exc:
            catalog_map = {}
            catalog_warning = f"非东方财富代码无法映射到板块成分股接口：{type(exc).__name__}: {exc}"
        else:
            catalog_warning = None
    else:
        catalog_warning = None

    leaders: list[dict[str, Any]] = []
    warnings: list[str] = []
    if catalog_warning:
        warnings.append(catalog_warning)

    def fetch_one(item: dict[str, Any]) -> dict[str, Any]:
        code = _board_code(item, catalog_map)
        if not code:
            raise RuntimeError("无法将当前板块代码或名称映射到公开成分股接口")
        return {
            **item,
            **_pick_leaders(_fetch_constituents(code)),
            "leader_data_source": "东方财富公开板块成分股行情",
            "eastmoney_board_code": code,
            "provider": provider_name,
        }

    with ThreadPoolExecutor(max_workers=min(4, len(items))) as executor:
        futures = {executor.submit(fetch_one, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                leaders.append(future.result())
            except Exception as exc:
                warnings.append(
                    f"{item['sector_name']}（{item['sector_code']}）："
                    f"{type(exc).__name__}: {exc}"
                )

    order = {item["sector_code"]: index for index, item in enumerate(items)}
    leaders.sort(key=lambda item: order.get(item["sector_code"], 999))
    return {
        "leaders": leaders,
        "warnings": warnings[:20],
        "source": "东方财富公开板块成分股行情",
        "definition": "龙头股按总市值优先，领涨股按最新涨跌幅；公开字段缺失时会显示回退口径。",
    }
