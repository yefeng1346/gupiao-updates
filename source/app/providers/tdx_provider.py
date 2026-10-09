from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
import os
import re
import struct
import threading
from typing import Callable, Iterable
from zipfile import BadZipFile, ZipFile

import pandas as pd

from app.providers.quote_fallback import fetch_resilient_quotes


_TDX_CODE_RE = re.compile(r"^88\d{4}$")
_DAY_RECORD = struct.Struct("<5if2i")
_DEFAULT_SERVERS = (
    # These are public TDX quotation servers that currently return both the
    # zhb.zip board bundle and index K-lines. Keep a few fallbacks because
    # availability depends on the user's network/operator.
    "180.153.18.170:7709",
    "60.191.117.167:7709",
    "218.75.126.9:7709",
    "115.238.56.198:7709",
)


class TdxProvider:
    """Read 通达信 board index catalogs and daily bars.

    The preferred path is local TDX data because it works without a separate
    API key and preserves the exact 880xxx code system used by the desktop
    client. If a local installation is not available, the provider can fall
    back to the public TDX quotation protocol when the network permits it.
    """

    name = "tdx"
    code_system = "通达信板块指数"

    def __init__(
        self,
        workers: int = 2,
        root: str | Path | None = None,
        servers: Iterable[str] | None = None,
        local_only: bool = False,
        remote_only: bool = False,
    ):
        self.workers = max(1, workers)
        self.local_only = bool(local_only)
        self.remote_only = bool(remote_only)
        root_text = str(root or os.getenv("TDX_ROOT", "")).strip()
        self.roots = self._resolve_roots(root_text)
        self.servers = self._parse_servers(
            servers if servers is not None else os.getenv("TDX_SERVERS", "")
        )
        self._catalog_cache: dict[str, list[dict]] = {}
        self._day_file_cache: dict[str, Path | None] = {}
        self._api = None
        self._api_lock = threading.Lock()
        self._network_unavailable = False

    def close(self) -> None:
        """Close the shared quotation connection and its heartbeat thread."""
        with self._api_lock:
            api = self._api
            self._api = None
            self._network_unavailable = False
            if api is not None:
                try:
                    api.disconnect()
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @staticmethod
    def _resolve_roots(root_text: str) -> list[Path]:
        candidates: list[Path] = []
        if root_text:
            candidates.append(Path(root_text).expanduser())
        else:
            # Only use common locations when no explicit root is configured.
            # Otherwise an older second installation could silently win.
            for raw in (
                r"C:\TDX",
                r"D:\TDX",
                r"E:\TDX",
                r"C:\new_tdx",
                r"D:\new_tdx",
                r"C:\通达信",
                r"D:\通达信",
                r"C:\Program Files\TongDaXin",
                r"D:\Program Files\TongDaXin",
                r"C:\Program Files (x86)\TongDaXin",
                r"D:\Program Files (x86)\TongDaXin",
            ):
                candidates.append(Path(raw))

        result: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                key = str(candidate.resolve()).lower()
            except OSError:
                key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                result.append(candidate)
        return result

    @staticmethod
    def _parse_servers(value: Iterable[str] | str) -> tuple[tuple[str, int], ...]:
        if isinstance(value, str):
            raw_items = [item for item in re.split(r"[,;\s]+", value.strip()) if item]
        else:
            raw_items = [str(item).strip() for item in value if str(item).strip()]
        if not raw_items:
            raw_items = list(_DEFAULT_SERVERS)

        result: list[tuple[str, int]] = []
        for item in raw_items:
            if ":" in item:
                host, port_text = item.rsplit(":", 1)
                try:
                    port = int(port_text)
                except ValueError:
                    port = 7709
            else:
                host, port = item, 7709
            if host:
                result.append((host, port))
        return tuple(result)

    @staticmethod
    def _decode(raw: bytes) -> str:
        for encoding in ("gb18030", "gbk", "utf-8-sig", "utf-8"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("gb18030", errors="replace")

    @staticmethod
    def _normalize_code(value: object) -> str:
        code = str(value or "").strip().upper()
        if code.endswith(".0"):
            code = code[:-2]
        return code

    def _catalog_paths(self) -> list[Path]:
        paths: list[Path] = []
        for root in self.roots:
            paths.extend(
                (
                    root / "T0002" / "hq_cache" / "tdxzs.cfg",
                    root / "T0002" / "hq_cache" / "tdxzs3.cfg",
                    root / "hq_cache" / "tdxzs.cfg",
                    root / "hq_cache" / "tdxzs3.cfg",
                    root / "tdxzs.cfg",
                )
            )
        return paths

    @staticmethod
    def _parse_catalog_text(text: str, sector_type: str) -> list[dict]:
        allowed_types = {"4"} if sector_type == "concept" else {"2", "8"}
        result: list[dict] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            fields = [item.strip() for item in raw_line.strip().split("|")]
            if len(fields) < 3:
                continue
            name = fields[0]
            code = TdxProvider._normalize_code(fields[1])
            category = fields[2]
            if not name or not _TDX_CODE_RE.fullmatch(code) or category not in allowed_types:
                continue
            if code in seen:
                continue
            seen.add(code)
            result.append({"sector_code": code, "sector_name": name})
        return result

    def _read_local_catalog(self, sector_type: str) -> list[dict]:
        rows: list[dict] = []
        seen_paths: set[str] = set()
        for path in self._catalog_paths():
            key = str(path).lower()
            if key in seen_paths or not path.is_file():
                continue
            seen_paths.add(key)
            try:
                rows.extend(self._parse_catalog_text(path.read_bytes().decode("gb18030"), sector_type))
            except (OSError, UnicodeDecodeError):
                try:
                    rows.extend(self._parse_catalog_text(self._decode(path.read_bytes()), sector_type))
                except OSError:
                    continue

        unique: dict[str, dict] = {}
        for row in rows:
            unique.setdefault(row["sector_code"], row)
        return list(unique.values())

    def _connect(self):
        if self._network_unavailable:
            raise RuntimeError("通达信行情服务器当前不可连接")
        with self._api_lock:
            if self._api is not None:
                return self._api
            try:
                from pytdx.hq import TdxHq_API
            except ImportError as exc:
                self._network_unavailable = True
                raise RuntimeError("未安装 pytdx，无法连接通达信行情协议") from exc

            for host, port in self.servers:
                api = TdxHq_API()
                try:
                    if api.connect(host, port, time_out=4):
                        self._api = api
                        return api
                except Exception:
                    try:
                        api.disconnect()
                    except Exception:
                        pass
            self._network_unavailable = True
            raise RuntimeError("通达信行情服务器连接失败，请检查网络或配置 TDX_SERVERS")

    def _read_network_catalog(self) -> bytes:
        api = self._connect()

        # Some TDX quotation servers expose the board mapping only inside the
        # post-market configuration bundle.  A direct tdxzs.cfg request may
        # legally return an empty file, so keep it as a fast path and then
        # fall back to the documented zhb.zip package.
        chunks: list[bytes] = []
        offset = 0
        for _ in range(256):
            response = api.get_report_file("tdxzs.cfg", offset)
            if not response or not response.get("chunksize"):
                break
            size = int(response["chunksize"])
            chunk = bytes(response.get("chunkdata", b""))[:size]
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            if len(chunk) < size:
                break
        raw = b"".join(chunks)
        if raw:
            return raw

        try:
            bundle = bytes(api.get_report_file_by_size("zhb.zip"))
        except Exception as exc:
            raise RuntimeError("通达信接口没有返回 tdxzs.cfg 或 zhb.zip") from exc
        try:
            catalog = self._catalog_bytes_from_zhb(bundle)
        except (BadZipFile, OSError, RuntimeError) as exc:
            raise RuntimeError("通达信 zhb.zip 中没有可解析的 tdxzs.cfg") from exc
        if not catalog:
            raise RuntimeError("通达信 zhb.zip 中没有可解析的 tdxzs.cfg")
        return catalog

    @staticmethod
    def _catalog_bytes_from_zhb(bundle: bytes) -> bytes:
        """Extract the two board-index mapping files from a zhb.zip bundle."""
        if not bundle:
            return b""
        pieces: list[bytes] = []
        with ZipFile(BytesIO(bundle)) as archive:
            for wanted in ("tdxzs.cfg", "tdxzs3.cfg"):
                for member in archive.namelist():
                    if Path(member).name.lower() != wanted:
                        continue
                    data = archive.read(member)
                    if data:
                        pieces.append(data)
                    break
        return b"\n".join(pieces)

    def list_sectors(self, sector_type: str) -> list[dict]:
        if sector_type not in {"concept", "industry"}:
            raise ValueError(f"不支持的板块类型：{sector_type}")
        if sector_type in self._catalog_cache:
            return self._catalog_cache[sector_type]

        local = [] if self.remote_only else self._read_local_catalog(sector_type)
        if local:
            self._catalog_cache[sector_type] = local
            return local

        if self.local_only:
            root_hint = os.getenv("TDX_ROOT", "").strip() or "自动检测路径"
            raise RuntimeError(
                "通达信普通版本地目录不可用：未找到 tdxzs.cfg。"
                f"请在 .env 设置 TDX_ROOT 为通达信普通版根目录（当前：{root_hint}）。"
            )

        try:
            remote = self._parse_catalog_text(
                self._decode(self._read_network_catalog()), sector_type
            )
        except Exception as exc:
            root_hint = os.getenv("TDX_ROOT", "未设置")
            raise RuntimeError(
                f"通达信板块目录不可用：未找到本地 tdxzs.cfg，且远程协议获取失败：{exc}。"
                f"请在 .env 设置 TDX_ROOT（当前：{root_hint}）。"
            ) from exc
        if not remote:
            raise RuntimeError(f"通达信目录没有返回 {sector_type} 的 880xxx 板块")
        self._catalog_cache[sector_type] = remote
        return remote

    def _find_day_file(self, code: str) -> Path | None:
        if code in self._day_file_cache:
            return self._day_file_cache[code]
        names = (f"sz{code}.day", f"sh{code}.day", f"{code}.day")
        candidates: list[Path] = []
        for root in self.roots:
            for name in names:
                candidates.extend(
                    (
                        root / "vipdoc" / "sz" / "lday" / name,
                        root / "vipdoc" / "sh" / "lday" / name,
                        root / "T0002" / "hq_cache" / name,
                        root / "hq_cache" / name,
                        root / name,
                    )
                )
        for path in candidates:
            if path.is_file():
                self._day_file_cache[code] = path
                return path

        # Installation layouts differ. A bounded exact-name search is used
        # only after the standard paths fail and is cached per code.
        for root in self.roots:
            if not root.is_dir():
                continue
            for name in names:
                try:
                    match = next(root.rglob(name), None)
                except OSError:
                    match = None
                if match and match.is_file():
                    self._day_file_cache[code] = match
                    return match
        self._day_file_cache[code] = None
        return None

    @staticmethod
    def _day_rows(path: Path, sector_type: str, code: str, name: str, start_date: date, end_date: date) -> list[dict]:
        rows: list[dict] = []
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_DAY_RECORD.size)
                    if len(chunk) < _DAY_RECORD.size:
                        break
                    raw_date, open_value, high_value, low_value, close_value, amount, volume, _ = _DAY_RECORD.unpack(chunk)
                    try:
                        trade_date = datetime.strptime(str(raw_date), "%Y%m%d").date()
                    except ValueError:
                        continue
                    if not start_date <= trade_date <= end_date:
                        continue
                    rows.append(
                        {
                            "trade_date": trade_date.isoformat(),
                            "sector_type": sector_type,
                            "sector_code": code,
                            "sector_name": name,
                            "close": close_value / 100,
                            "pct_change": None,
                            "amount": float(amount),
                            "volume": int(volume),
                            "data_source": "tdx_local",
                        }
                    )
        except OSError as exc:
            raise RuntimeError(f"无法读取通达信日线文件：{path}") from exc
        rows.sort(key=lambda row: row["trade_date"])
        previous_close = None
        for row in rows:
            if previous_close not in (None, 0):
                row["pct_change"] = (row["close"] / previous_close - 1) * 100
            previous_close = row["close"]
        return rows

    @staticmethod
    def _network_rows(
        bars: list[dict],
        sector_type: str,
        code: str,
        name: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        rows: list[dict] = []
        for item in bars or []:
            raw_date = item.get("datetime") or item.get("date")
            if isinstance(raw_date, datetime):
                trade_date = raw_date.date()
            else:
                text = str(raw_date or "")[:10]
                try:
                    trade_date = date.fromisoformat(text)
                except ValueError:
                    continue
            if not start_date <= trade_date <= end_date:
                continue
            rows.append(
                {
                    "trade_date": trade_date.isoformat(),
                    "sector_type": sector_type,
                    "sector_code": code,
                    "sector_name": name,
                    "close": item.get("close"),
                    "pct_change": None,
                    "amount": item.get("amount"),
                    "volume": item.get("vol", item.get("volume")),
                    "data_source": "tdx_protocol",
                }
            )
        rows.sort(key=lambda row: row["trade_date"])
        previous_close = None
        for row in rows:
            try:
                close = float(row["close"])
            except (TypeError, ValueError):
                close = None
            if close is not None and previous_close not in (None, 0):
                row["pct_change"] = (close / previous_close - 1) * 100
            if close is not None:
                previous_close = close
        return rows

    def _fetch_network_history(
        self,
        sector_type: str,
        code: str,
        name: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        api = self._connect()
        with self._api_lock:
            for market in (0, 1):
                bars = api.get_index_bars(9, market, code, 0, 800)
                rows = self._network_rows(bars, sector_type, code, name, start_date, end_date)
                if rows:
                    return rows
        raise RuntimeError(f"通达信没有返回板块指数 {code} 的历史 K 线")

    def fetch_sector_history(
        self,
        sector_type: str,
        sector_code: str,
        sector_name: str,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        code = self._normalize_code(sector_code)
        path = None if self.remote_only else self._find_day_file(code)
        if path:
            rows = self._day_rows(path, sector_type, code, sector_name, start_date, end_date)
            if rows:
                return rows
        if self.local_only:
            source_hint = f"本地文件 {path}" if path else "未找到本地 .day 文件"
            raise RuntimeError(
                f"通达信普通版板块 {code} 历史行情不可用：{source_hint}；"
                "请确认 TDX_ROOT 指向普通版根目录，并先让通达信完成板块数据更新。"
            )
        try:
            return self._fetch_network_history(
                sector_type, code, sector_name, start_date, end_date
            )
        except Exception as exc:
            source_hint = f"本地文件 {path}" if path else "未找到本地 .day 文件"
            raise RuntimeError(f"通达信板块 {code} 历史行情不可用：{source_hint}；协议接口：{exc}") from exc

    def sync_sector_history(
        self,
        sector_type: str,
        calendar_days: int = 120,
        max_sectors: int = 0,
        sector_codes: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        cached_sectors: list[dict] | None = None,
    ) -> dict:
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
        catalog_source = "live"
        catalog_warning = None
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
                catalog_warning = f"实时通达信目录不可用，已使用本地目录缓存：{live_error}"

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
                raise RuntimeError("没有在通达信板块目录中找到输入的代码/名称")
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
            "catalog_source": catalog_source,
            "rows": rows,
            "errors": errors,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    def realtime_quotes(self, symbols: list[str]) -> list[dict]:
        normalized = [self._normalize_code(symbol) for symbol in symbols if str(symbol).strip()]
        if not normalized:
            return []

        # The HTTP quote fallbacks are designed for stocks and do not return
        # 通达信 880xxx board indices. Query those indices through the same TDX
        # protocol used by the history sync path, so the live page can refresh
        # the exact board codes shown in the report.
        board_codes = [code for code in normalized if _TDX_CODE_RE.fullmatch(code)]
        stock_codes = [code for code in normalized if code not in board_codes]
        rows: list[dict] = []
        errors: list[str] = []

        if board_codes:
            try:
                api = self._connect()
                with self._api_lock:
                    quotes = api.get_security_quotes([(1, code) for code in board_codes]) or []
                returned = {self._normalize_code(row.get("code")): row for row in quotes if row}
                for code in board_codes:
                    row = returned.get(code)
                    if row is None:
                        continue
                    price = self._quote_number(row.get("price"))
                    previous = self._quote_number(row.get("last_close"))
                    change = None if price is None or previous is None else price - previous
                    pct_change = None
                    if change is not None and previous not in (None, 0):
                        pct_change = change / previous * 100
                    rows.append(
                        {
                            "代码": code,
                            "最新价": price,
                            "昨收": previous,
                            "今开": self._quote_number(row.get("open")),
                            "最高": self._quote_number(row.get("high")),
                            "最低": self._quote_number(row.get("low")),
                            "涨跌额": change,
                            "涨跌幅": pct_change,
                            "成交量": self._quote_number(row.get("vol")),
                            "成交额": self._quote_number(row.get("amount")),
                            "行情时间": row.get("servertime"),
                            "数据源": "tdx_quote",
                        }
                    )
                missing = [code for code in board_codes if code not in returned]
                if missing:
                    errors.append(f"通达信未返回板块：{', '.join(missing)}")
            except Exception as exc:
                errors.append(f"通达信板块实时行情失败：{type(exc).__name__}: {exc}")

        if stock_codes:
            try:
                rows.extend(fetch_resilient_quotes(stock_codes))
            except Exception as exc:
                errors.append(f"个股实时行情失败：{type(exc).__name__}: {exc}")

        if rows:
            return rows
        raise RuntimeError("；".join(errors) or "实时行情没有返回数据")

    @staticmethod
    def _quote_number(value: object) -> float | None:
        if value in (None, "", "-"):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if number != number else number


class TdxStandardProvider(TdxProvider):
    """Use the local files written by the 通达信普通版 installation.

    The ordinary desktop client stores board catalogs under ``T0002`` and
    board index daily files under ``vipdoc``.  This provider deliberately does
    not fall back to the remote quotation protocol for catalog/history data,
    so a missing local file is reported clearly instead of silently changing
    the data source.
    """

    name = "tdx_standard"

    def __init__(
        self,
        workers: int = 2,
        root: str | Path | None = None,
        servers: Iterable[str] | None = None,
    ):
        super().__init__(
            workers=workers,
            root=root,
            servers=servers,
            local_only=True,
        )


class TdxOnlineProvider(TdxProvider):
    """Use the public TDX quotation protocol without reading local files."""

    name = "tdx_online"

    def __init__(
        self,
        workers: int = 2,
        root: str | Path | None = None,
        servers: Iterable[str] | None = None,
    ):
        super().__init__(
            workers=workers,
            root=root,
            servers=servers,
            remote_only=True,
        )
