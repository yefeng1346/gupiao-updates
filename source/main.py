from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import os
import json
from pathlib import Path
from queue import Empty, Queue
import re
from threading import Event, Thread, Lock
import time
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.analytics import build_report, recalculate_metrics
from app.capital_flow import (
    fetch_sector_capital_flow,
    fetch_sector_capital_flow_report,
    present_sector_capital_flow_report,
    share_current_flow_with_history,
    validate_flow_rule,
    flow_report_with_rule,
)
from app.flow_calendar import shanghai_now, latest_closed_trading_day, SHANGHAI, is_trading_day
from app.flow_import import import_flow, imported_flow_report, TEMPLATE
from app.flow_tasks import FlowHistoryJobs
from app.flow_archive import DailyCollector
from app.config import ensure_directories, settings
from app.db import Database
from app.llm import ArkClient
from app.providers.akshare_provider import AkShareProvider
from app.providers.efinance_provider import EFinanceProvider
from app.providers.tdx_provider import TdxOnlineProvider, TdxProvider, TdxStandardProvider
from app.sector_leaders import fetch_sector_constituents, fetch_sector_leaders
from app.formula_screen import DEFAULT_FORMULA, screen_formula, validate_formula
from app.snapshot_import import SnapshotValidationError, normalize_snapshot_rows
from app.update_service import (
    CURRENT_VERSION,
    UpdateError,
    get_update_info,
    launch_updater,
    parse_manifest,
    UPDATE_PREPARATION,
)


ensure_directories()
database = Database(settings.database_path)
app = FastAPI(title="本地股票复盘工具", version=CURRENT_VERSION)
web_dir = settings.asset_dir / "web"
app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")
DEFAULT_FORMULA_NAME = "默认策略：OBV角度 + 资金上穿"
database.ensure_formula(DEFAULT_FORMULA_NAME, DEFAULT_FORMULA, is_builtin=True)
_auto_sync_stop = Event()
_auto_sync_thread: Thread | None = None
_auto_sync_last_runs: dict[str, str] = {}
SUPPORTED_MARKET_PROVIDERS = {
    "akshare",
    "efinance",
    "tdx",
    "tdx_online",
    "tdx_standard",
}
MARKET_PROVIDER_LABELS = {
    "akshare": "AkShare",
    "efinance": "efinance",
    "tdx": "通达信协议/本地文件",
    "tdx_online": "通达信网络数据",
    "tdx_standard": "通达信普通版（本地文件）",
}
ProviderName = Literal["akshare", "efinance", "tdx", "tdx_online", "tdx_standard"]


def llm_model_options() -> list[dict[str, str]]:
    """Return the configured LLM choices without exposing the API key."""
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    configured = [
        (settings.ark_model, "DeepSeek V4 Pro"),
        (settings.ark_doubao_model, "Doubao-Seed-2.1-pro"),
    ]
    for model, label in configured:
        model = str(model or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        options.append({"value": model, "label": label})
    return options


def llm_model_label(model: str) -> str:
    for option in llm_model_options():
        if option["value"] == model:
            return option["label"]
    return model


def normalize_llm_model(value: str | None = None) -> str:
    model = str(value or settings.ark_model).strip()
    if not model:
        raise HTTPException(status_code=400, detail="没有配置可用的 LLM 模型")
    supported = {option["value"] for option in llm_model_options()}
    if model not in supported:
        labels = "、".join(option["label"] for option in llm_model_options())
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 LLM 模型：{model}；可选模型：{labels}",
        )
    return model


def llm_api_key_for_model(model: str) -> str:
    """Select a model-specific key while preserving the old shared-key setup."""
    if model == settings.ark_doubao_model:
        return settings.ark_doubao_api_key or settings.ark_api_key
    return settings.ark_api_key


def llm_key_hint_for_model(model: str) -> str:
    if model == settings.ark_doubao_model:
        return "ARK_DOUBAO_API_KEY（未填写时可回退 ARK_API_KEY）"
    return "ARK_API_KEY"


class SyncRequest(BaseModel):
    provider: ProviderName | None = None
    sector_type: Literal["concept", "industry"] = settings.default_sector_type  # type: ignore[assignment]
    calendar_days: int = Field(default=120, ge=60, le=500)
    max_sectors: int = Field(default=0, ge=0, le=1000)
    # Optional comma/newline separated board codes or exact board names.  When
    # empty, the provider uses its complete universe and max_sectors applies.
    sector_codes: str = Field(default="", max_length=5000)


class CatalogSyncRequest(BaseModel):
    provider: ProviderName | None = None
    sector_type: Literal["concept", "industry"] = settings.default_sector_type  # type: ignore[assignment]


class SectorLeaderItem(BaseModel):
    sector_code: str = Field(min_length=1, max_length=32)
    sector_name: str = Field(min_length=1, max_length=100)


class SectorLeadersRequest(BaseModel):
    provider: ProviderName | None = None
    sector_type: Literal["concept", "industry"] = settings.default_sector_type  # type: ignore[assignment]
    sectors: list[SectorLeaderItem] = Field(min_length=1, max_length=20)


class SectorConstituentsRequest(BaseModel):
    provider: ProviderName | None = None
    sector_type: Literal["concept", "industry"] = settings.default_sector_type  # type: ignore[assignment]
    sector_code: str = Field(default="", max_length=32)
    sector_name: str = Field(default="", max_length=100)


class FormulaScreenRequest(BaseModel):
    provider: ProviderName | None = None
    formula_id: int | None = Field(default=None, ge=1)
    formula_name: str | None = Field(default=None, max_length=100)
    formula: str | None = Field(default=None, min_length=1, max_length=30000)
    timeframe: Literal["5m", "15m", "30m", "60m", "daily", "weekly"] = "weekly"
    max_results: int = Field(default=100, ge=1, le=300)


class FormulaDefinitionRequest(BaseModel):
    id: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=100)
    formula: str = Field(min_length=1, max_length=30000)


class LLMRequest(BaseModel):
    sector_type: Literal["concept", "industry"] = settings.default_sector_type  # type: ignore[assignment]
    report_date: str | None = None
    model: str | None = Field(default=None, max_length=200)
    provider: ProviderName | None = None


class ClearRequest(BaseModel):
    sector_type: Literal["concept", "industry", "all"] = "all"


class SnapshotRowRequest(BaseModel):
    trade_date: str = Field(min_length=10, max_length=10)
    sector_type: Literal["concept", "industry"]
    sector_code: str = Field(min_length=1, max_length=32)
    sector_name: str = Field(min_length=1, max_length=100)
    rank: int
    rps50: float
    pct_change: float
    close: float
    amount: float
    volume: float | None = None


class SnapshotImportRequest(BaseModel):
    rows: list[SnapshotRowRequest] = Field(min_length=1, max_length=50000)


def normalize_provider_name(value: str | None = None) -> str:
    name = str(value or settings.market_provider).strip().lower()
    if name not in SUPPORTED_MARKET_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的行情源：{name}；可选："
                "akshare、efinance、tdx、tdx_online、tdx_standard"
            ),
        )
    return name


def provider(
    provider_name: str | None = None,
) -> AkShareProvider | EFinanceProvider | TdxProvider | TdxOnlineProvider | TdxStandardProvider:
    active_provider = normalize_provider_name(provider_name)
    if active_provider == "efinance":
        return EFinanceProvider(workers=settings.akshare_workers)  # type: ignore[return-value]
    if active_provider in {"tdx", "tdx_online", "tdx_standard"}:
        provider_class = (
            TdxStandardProvider
            if active_provider == "tdx_standard"
            else TdxOnlineProvider
            if active_provider == "tdx_online"
            else TdxProvider
        )
        return provider_class(
            workers=settings.akshare_workers,
            root=settings.tdx_root or None,
            servers=settings.tdx_servers or None,
        )
    if active_provider != "akshare":
        raise HTTPException(
            status_code=501,
            detail=f"当前暂未实现行情源：{active_provider}",
        )
    return AkShareProvider(workers=settings.akshare_workers)


def close_provider(source) -> None:
    close = getattr(source, "close", None)
    if callable(close):
        close()


def make_report(
    sector_type: str,
    report_date: str | None = None,
    provider_name: str | None = None,
) -> dict:
    dataset_id = normalize_provider_name(provider_name)
    try:
        return build_report(
            database,
            sector_type,
            settings.rps_window,
            report_date,
            dataset_id=dataset_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def parse_sector_codes(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\s,，]+", value.strip()) if item.strip()][:100]


def parse_search_terms(value: str) -> list[str]:
    """Split a board search into up to 100 comma/space/newline terms."""
    return [item.strip() for item in re.split(r"[\s,，]+", value.strip()) if item.strip()][:100]


def configured_auto_sync_types() -> list[str]:
    values = [item.strip().lower() for item in re.split(r"[,，\s]+", settings.auto_close_sync_types)]
    return [item for item in values if item in {"concept", "industry"}]


def configured_code_systems(provider_name: str | None = None) -> list[str]:
    """Return catalog systems relevant to the configured provider."""
    active_provider = normalize_provider_name(provider_name)
    providers = {
        "akshare": AkShareProvider,
        "efinance": EFinanceProvider,
        "tdx": TdxProvider,
        "tdx_online": TdxOnlineProvider,
        "tdx_standard": TdxStandardProvider,
    }
    source = providers[active_provider]
    systems = [source.code_system, "结构化板块快照"]
    if active_provider == "efinance":
        systems.append(AkShareProvider.code_system)
    return list(dict.fromkeys(systems))


def catalog_rows(source, sector_type: str, sectors: list[dict]) -> list[dict]:
    code_system = getattr(source, "code_system", source.name)
    return [
        {
            "dataset_id": source.name,
            "sector_type": sector_type,
            "sector_code": sector["sector_code"],
            "sector_name": sector["sector_name"],
            "code_system": code_system,
            "data_source": source.name,
        }
        for sector in sectors
    ]


def cached_catalog(source, sector_type: str) -> list[dict]:
    code_system = getattr(source, "code_system", source.name)
    rows = database.get_sector_catalog(
        sector_type, code_system, dataset_id=source.name
    )
    if rows:
        return rows
    # efinance is intentionally a compatibility provider: its normal path
    # delegates board data to AkShare, so either compatible catalog is safe.
    if getattr(source, "name", "") == "efinance":
        compatible = database.get_sector_catalog(
            sector_type, "AkShare板块代码", dataset_id=source.name
        )
        if compatible:
            return compatible
        return database.get_sector_catalog(
            sector_type, "efinance板块代码", dataset_id=source.name
        )
    # Never mix code systems. In particular, AkShare/BK codes must not be
    # passed to the TDX provider as if they were 880xxx indices.
    return []


def call_with_timeout(func, timeout: float):
    """Run an upstream call without allowing a broken network client to hang the API."""
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def run():
        try:
            result.put((True, func()))
        except BaseException as exc:
            result.put((False, exc))

    worker = Thread(target=run, daemon=True)
    worker.start()
    try:
        succeeded, value = result.get(timeout=timeout)
    except Empty as exc:
        raise TimeoutError(f"上游接口超过 {timeout:g} 秒没有响应") from exc
    if succeeded:
        return value
    raise value  # type: ignore[misc]


def _reuse_cached_history(
    source_name: str,
    sector_type: str,
    selected_catalog: list[dict[str, Any]],
    fresh_rows: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep an existing board history when a public network node is down.

    This is deliberately limited to network providers.  A local TDX source
    should report missing local files instead of silently falling back to a
    possibly stale database snapshot.
    """
    if source_name not in {"akshare", "efinance"} or not selected_catalog:
        return [], []
    fresh_codes = {
        str(row.get("sector_code") or "").strip().lower()
        for row in fresh_rows
        if str(row.get("sector_code") or "").strip()
    }
    cached_rows = database.get_sector_rows(
        sector_type, start_date, end_date, dataset_id=source_name
    )
    rows_by_code: dict[str, list[dict[str, Any]]] = {}
    for row in cached_rows:
        code = str(row.get("sector_code") or "").strip().lower()
        if code:
            rows_by_code.setdefault(code, []).append(row)

    reused: list[dict[str, Any]] = []
    fallback_rows: list[dict[str, Any]] = []
    for sector in selected_catalog:
        code = str(sector.get("sector_code") or "").strip()
        code_key = code.lower()
        if not code_key or code_key in fresh_codes:
            continue
        rows = rows_by_code.get(code_key, [])
        if not rows:
            continue
        reused.append(sector)
        for row in rows:
            copy = dict(row)
            copy["data_source"] = "cached_market_data"
            fallback_rows.append(copy)
    return fallback_rows, reused


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(str(web_dir / "index.html"))


@app.get("/api/update/check")
def update_check():
    """Check a public HTTPS manifest without delaying normal page startup."""
    return {"ok": True, **get_update_info(settings.update_manifest_url)}


def _exit_after_update_launch() -> None:
    # Let the HTTP response reach the webview before the parent process exits.
    # GupiaoUpdater.exe waits for this PID before replacing program files.
    time.sleep(1.0)
    os._exit(0)


@app.post("/api/update/apply")
def update_apply():
    """Launch the standalone updater after revalidating the manifest."""
    info = get_update_info(settings.update_manifest_url)
    if info.get("error"):
        raise HTTPException(status_code=502, detail=str(info["error"]))
    if not info.get("configured"):
        raise HTTPException(status_code=400, detail="尚未配置 UPDATE_MANIFEST_URL")
    if not info.get("available"):
        raise HTTPException(status_code=409, detail="当前已经是最新版本")
    try:
        result = UPDATE_PREPARATION.start(parse_manifest(info["update"]))
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok":True,**result}


@app.get("/api/update/status")
def update_status():
    return {"ok":True,**UPDATE_PREPARATION.snapshot()}


@app.post("/api/update/cancel")
def update_cancel():
    return {"ok":True,**UPDATE_PREPARATION.cancel()}


@app.post("/api/update/install")
def update_install():
    if _flow_history_jobs.is_running():
        raise HTTPException(status_code=409,detail="请先取消正在执行的资金更新，再安装软件更新")
    try:
        result = UPDATE_PREPARATION.install(settings.root_dir,os.getpid())
    except (UpdateError,OSError,RuntimeError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    Thread(target=_exit_after_update_launch,daemon=True).start()
    return {"ok":True,**result}


@app.get("/api/health")
def health():
    dates = database.get_dates(
        settings.default_sector_type, dataset_id=settings.market_provider
    )
    warnings = []
    if settings.market_provider not in SUPPORTED_MARKET_PROVIDERS:
        warnings.append("MARKET_PROVIDER 必须是 akshare、efinance、tdx 或 tdx_standard")
    if settings.default_sector_type not in {"concept", "industry"}:
        warnings.append("DEFAULT_SECTOR_TYPE 必须是 concept 或 industry")
    if settings.market_provider == "tdx" and not settings.tdx_root:
        warnings.append(
            "当前使用 tdx，但没有配置 TDX_ROOT；将使用远程协议，网络不可用时无法读取本地 880xxx 文件"
        )
    if settings.market_provider == "tdx_standard" and not settings.tdx_root:
        warnings.append(
            "当前使用通达信普通版本地模式，但没有配置 TDX_ROOT；程序只会尝试少量常见安装目录"
        )
    if settings.market_provider == "tdx_online" and not settings.tdx_servers:
        warnings.append("当前使用通达信网络数据；如果连接失败，可在 .env 配置 TDX_SERVERS")
    return {
        "ok": True,
        "service": "gupiao",
        "app_version": CURRENT_VERSION,
        "update_configured": bool(settings.update_manifest_url),
        "update_check_on_startup": settings.update_check_on_startup,
        "market_provider": settings.market_provider,
        "market_provider_label": MARKET_PROVIDER_LABELS.get(
            settings.market_provider, settings.market_provider
        ),
        "market_provider_options": [
            {"value": value, "label": label}
            for value, label in MARKET_PROVIDER_LABELS.items()
        ],
        "default_sector_type": settings.default_sector_type,
        "ark_model": settings.ark_model,
        "ark_doubao_model": settings.ark_doubao_model,
        "ark_model_options": llm_model_options(),
        "ark_configured": bool(settings.ark_api_key or settings.ark_doubao_api_key),
        "ark_deepseek_key_configured": bool(settings.ark_api_key),
        "ark_doubao_key_configured": bool(
            settings.ark_doubao_api_key or settings.ark_api_key
        ),
        "ark_doubao_separate_key_configured": bool(settings.ark_doubao_api_key),
        "ark_timeout_seconds": settings.ark_timeout_seconds,
        "ark_use_env_proxy": settings.ark_use_env_proxy,
        "ark_thinking_type": settings.ark_thinking_type,
        "ark_max_tokens": settings.ark_max_tokens,
        "market_catalog_timeout_seconds": settings.market_catalog_timeout_seconds,
        "market_http_mode": settings.market_http_mode,
        "tdx_root_configured": bool(settings.tdx_root),
        "auto_sync_on_open": settings.auto_sync_on_open,
        "auto_close_sync": {
            "enabled": settings.auto_close_sync,
            "run_at": settings.auto_close_sync_time,
            "sector_types": configured_auto_sync_types(),
            "last_runs": dict(_auto_sync_last_runs),
        },
        "configuration": {
            "market_provider_supported": settings.market_provider in SUPPORTED_MARKET_PROVIDERS,
            "llm_configured": bool(
                settings.ark_api_key or settings.ark_doubao_api_key
            ),
            "warnings": warnings,
        },
        "database": str(settings.database_path),
        "dates": dates[:30],
        "catalog_counts": {
            "concept": database.count_sector_catalog(
                "concept", configured_code_systems(), dataset_id=settings.market_provider
            ),
            "industry": database.count_sector_catalog(
                "industry", configured_code_systems(), dataset_id=settings.market_provider
            ),
        },
    }


@app.get("/api/network-diagnose")
def network_diagnose():
    """Check the small public endpoints used by the network data source."""
    checks = [
        (
            "东方财富主节点",
            "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14",
        ),
        (
            "东方财富备用节点",
            "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14",
        ),
        (
            "同花顺板块目录",
            "https://q.10jqka.com.cn/gn/",
        ),
    ]
    results = []
    for name, url in checks:
        started = time.monotonic()
        try:
            response = AkShareProvider._direct_http_get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=(3, 6),
            )
            response.raise_for_status()
            results.append(
                {
                    "name": name,
                    "ok": True,
                    "status_code": response.status_code,
                    "bytes": len(response.content),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
            )
    passed = sum(1 for item in results if item["ok"])
    return {
        "ok": passed > 0,
        "market_http_mode": settings.market_http_mode,
        "checked": len(results),
        "passed": passed,
        "checks": results,
        "hint": (
            "备用公共节点可访问，主节点异常；程序会自动回退，通常不影响同步。"
            if any(item["ok"] and item["name"] == "东方财富备用节点" for item in results)
            and not all(item["ok"] for item in results)
            else "至少一个公共接口可访问；如果仍同步失败，可优先使用本地目录缓存或切换通达信普通版。"
            if passed
            else "公共目录接口都不可访问：通常是网络/VPN/代理或运营商线路问题，不是 ARK_API_KEY。可在 .env 设置 MARKET_HTTP_MODE=env 或 direct 后重试。"
        ),
    }


@app.post("/api/catalog/sync")
def sync_catalog(request: CatalogSyncRequest):
    source = provider(request.provider)
    try:
        # The directory button must fail over quickly; a live catalog refresh
        # is optional once the local catalog has been populated.
        sectors = call_with_timeout(
            lambda: source.list_sectors(request.sector_type),
            timeout=settings.market_catalog_timeout_seconds,
        )
        saved = database.upsert_sector_catalog(
            catalog_rows(source, request.sector_type, sectors)
        )
        return {
            "ok": True,
            "provider": source.name,
            "sector_type": request.sector_type,
            "catalog_count": saved,
            "catalog_source": "live",
        }
    except HTTPException:
        raise
    except Exception as exc:
        cached = cached_catalog(source, request.sector_type)
        if cached:
            return {
                "ok": True,
                "provider": source.name,
                "sector_type": request.sector_type,
                "catalog_count": len(cached),
                "catalog_source": "cache",
                "warning": f"实时目录更新失败，继续使用本地目录缓存：{type(exc).__name__}: {exc}",
            }
        raise HTTPException(
            status_code=502,
            detail=f"板块目录同步失败：{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        close_provider(source)


@app.post("/api/sync")
def sync(request: SyncRequest):
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source = provider(request.provider)
    try:
        result = source.sync_sector_history(
            request.sector_type,
            calendar_days=request.calendar_days,
            max_sectors=request.max_sectors,
            sector_codes=parse_sector_codes(request.sector_codes),
            cached_sectors=cached_catalog(source, request.sector_type),
        )
        cached_rows, cached_items = _reuse_cached_history(
            source.name,
            request.sector_type,
            result.get("selected_catalog", []),
            result.get("rows", []),
            result.get("start_date", ""),
            result.get("end_date", ""),
        )
        if cached_rows:
            result["rows"].extend(cached_rows)
            names = "、".join(
                str(item.get("sector_name") or item.get("sector_code"))
                for item in cached_items[:8]
            )
            if len(cached_items) > 8:
                names += "等"
            result.setdefault("errors", []).append(
                f"网络行情获取失败，已保留本地缓存：{names}（缓存可能不是最新数据）"
            )
        for row in result.get("catalog", []):
            row["dataset_id"] = source.name
        for row in result["rows"]:
            row["dataset_id"] = source.name
        catalog_saved = database.upsert_sector_catalog(result.get("catalog", []))
        saved_rows = database.upsert_sector_daily(result["rows"])
        recalculate_metrics(
            database,
            request.sector_type,
            settings.rps_window,
            dataset_id=source.name,
        )
        actual_dates = sorted(
            {
                str(row.get("trade_date") or "")
                for row in result["rows"]
                if str(row.get("trade_date") or "")
            }
        )
        actual_latest_date = actual_dates[-1] if actual_dates else None
        try:
            requested_end_is_weekday = datetime.fromisoformat(
                str(result["end_date"])
            ).weekday() < 5
        except (TypeError, ValueError):
            requested_end_is_weekday = False
        latest_coverage = len(
            {
                str(row.get("sector_code") or "")
                for row in result["rows"]
                if actual_latest_date and str(row.get("trade_date")) == actual_latest_date
            }
        )
        database.save_sync_run(
            request.sector_type,
            source.name,
            result["requested_sectors"],
            result["succeeded_sectors"],
            saved_rows,
            result["errors"],
            started_at,
        )
        selected = int(result["selected_sectors"])
        succeeded = int(result["succeeded_sectors"])
        cached_count = len(cached_items)
        effective_succeeded = min(selected, succeeded + cached_count)
        if succeeded == 0 and cached_count == selected and selected > 0:
            sync_status = "cached"
            sync_message = (
                f"网络暂时不可用；已保留本地缓存：{cached_count}/{selected} 个板块，"
                "数据可能不是最新收盘数据。"
            )
        elif succeeded == 0 and selected > 0:
            sync_status = "failed"
            sync_message = "没有成功获取任何板块，请检查网络或更换数据源后重试。"
        elif effective_succeeded < selected:
            sync_status = "partial"
            sync_message = (
                f"部分成功：实时 {succeeded} 个、缓存 {cached_count} 个，"
                f"共覆盖 {effective_succeeded}/{selected} 个板块；失败项可在下方错误列表查看。"
            )
        elif (
            source.name in {"tdx", "tdx_standard"}
            and requested_end_is_weekday
            and actual_latest_date
            and actual_latest_date < str(result["end_date"])
        ):
            sync_status = "stale"
            sync_message = (
                f"读取成功 {succeeded}/{selected} 个板块，但本地文件最新交易日为 "
                f"{actual_latest_date}，本次没有读到 {result['end_date']} 的记录。"
            )
        else:
            sync_status = "success"
            sync_message = f"全部成功：{succeeded}/{selected} 个板块。"
        return {
            "ok": sync_status != "failed",
            "provider": source.name,
            "sector_type": request.sector_type,
            "requested_sectors": result["requested_sectors"],
            "selected_sectors": result["selected_sectors"],
            "succeeded_sectors": result["succeeded_sectors"],
            "cached_sectors": cached_count,
            "saved_rows": saved_rows,
            "catalog_saved": catalog_saved,
            "catalog_source": result.get("catalog_source", "live"),
            "actual_latest_date": actual_latest_date,
            "latest_date_coverage": latest_coverage,
            "requested_end_date": result["end_date"],
            "sync_status": sync_status,
            "message": sync_message,
            "errors": result["errors"][:20],
            "date_range": [result["start_date"], result["end_date"]],
            "sector_filter": parse_sector_codes(request.sector_codes),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"行情同步失败：{type(exc).__name__}: {exc}") from exc
    finally:
        close_provider(source)


def _auto_sync_loop() -> None:
    while not _auto_sync_stop.is_set():
        now = datetime.now()
        try:
            hour_text, minute_text = settings.auto_close_sync_time.split(":", 1)
            target_minutes = int(hour_text) * 60 + int(minute_text)
        except (TypeError, ValueError):
            target_minutes = 15 * 60 + 5
        current_minutes = now.hour * 60 + now.minute
        if current_minutes >= target_minutes:
            today = now.date().isoformat()
            for sector_type in configured_auto_sync_types():
                key = f"{sector_type}:{today}"
                if key in _auto_sync_last_runs:
                    continue
                try:
                    result = sync(SyncRequest(sector_type=sector_type))
                    _auto_sync_last_runs[key] = (
                        f"{today} {result.get('sync_status', 'unknown')}"
                    )
                except Exception as exc:
                    _auto_sync_last_runs[key] = f"{today} failed: {type(exc).__name__}: {exc}"
        _auto_sync_stop.wait(30)


@app.on_event("startup")
def start_auto_sync() -> None:
    global _auto_sync_thread
    if not settings.auto_close_sync or _auto_sync_thread is not None:
        return
    _auto_sync_stop.clear()
    _auto_sync_thread = Thread(target=_auto_sync_loop, name="gupiao-auto-close-sync", daemon=True)
    _auto_sync_thread.start()


@app.on_event("shutdown")
def stop_auto_sync() -> None:
    global _auto_sync_thread
    _auto_sync_stop.set()
    thread = _auto_sync_thread
    _auto_sync_thread = None
    if thread is not None:
        thread.join(timeout=2)


@app.get("/api/search")
def search(
    sector_type: Literal["concept", "industry"] = settings.default_sector_type,  # type: ignore[assignment]
    query: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    provider_name: ProviderName | None = Query(default=None, alias="provider"),
):
    query = query.strip()
    terms = parse_search_terms(query)
    if not terms:
        raise HTTPException(status_code=400, detail="请输入板块代码或名称")
    active_provider = normalize_provider_name(provider_name)
    search_systems = configured_code_systems(active_provider)
    results, unmatched_queries = database.search_sectors_many(
        sector_type,
        terms,
        limit,
        code_systems=search_systems,
        dataset_id=active_provider,
    )
    if not results and len(terms) == 1 and re.fullmatch(r"88\d{4}", terms[0]):
        if active_provider in {"tdx", "tdx_online", "tdx_standard"}:
            message = (
                "当前已使用通达信代码体系，但目录中没有这个 880xxx；"
                "请确认 TDX_ROOT 指向通达信根目录且目录文件已更新。"
            )
        else:
            message = (
                "检测到通达信 88xxxx 板块指数代码；当前行情源使用 AkShare/efinance 代码体系。"
                "请在页面上切换到通达信网络数据或通达信普通版（本地文件）后重试。"
            )
    elif unmatched_queries:
        message = f"未找到：{'、'.join(unmatched_queries)}"
    else:
        message = (
            "未找到匹配项；如果还没有同步过板块目录，请先点击‘更新板块目录’。"
            if not results
            else ""
        )
    return {
        "ok": True,
        "sector_type": sector_type,
        "provider": active_provider,
        "provider_label": MARKET_PROVIDER_LABELS[active_provider],
        "query": query,
        "queries": terms,
        "unmatched_queries": unmatched_queries,
        "catalog_count": database.count_sector_catalog(
            sector_type, search_systems, dataset_id=active_provider
        ),
        "results": results,
        "message": message,
    }


@app.post("/api/import-snapshot")
def import_snapshot(request: SnapshotImportRequest):
    """Import source-provided rank/RPS values without deriving or guessing them."""
    try:
        rows = normalize_snapshot_rows(request.rows)
    except SnapshotValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    catalog = [
        {
            "dataset_id": "snapshot",
            "sector_type": row["sector_type"],
            "sector_code": row["sector_code"],
            "sector_name": row["sector_name"],
            "code_system": "结构化板块快照",
            "data_source": "snapshot_import",
        }
        for row in rows
    ]
    for row in rows:
        row["dataset_id"] = "snapshot"
    catalog_saved = database.upsert_sector_catalog(catalog)
    saved_rows = database.upsert_sector_daily(rows)
    for sector_type in sorted({row["sector_type"] for row in rows}):
        recalculate_metrics(
            database, sector_type, settings.rps_window, dataset_id="snapshot"
        )
    dates = sorted({row["trade_date"] for row in rows})
    return {
        "ok": True,
        "data_source": "snapshot_import",
        "rows": len(rows),
        "saved_rows": saved_rows,
        "catalog_saved": catalog_saved,
        "date_range": [dates[0], dates[-1]],
        "sector_types": sorted({row["sector_type"] for row in rows}),
        "message": "已原样保存结构化快照中的排名、RPS50 和行情字段；报告会标记为结构化快照。",
    }


@app.post("/api/clear")
def clear_data(request: ClearRequest):
    try:
        deleted = database.clear_sector_data(request.sector_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return {
        "ok": True,
        "sector_type": request.sector_type,
        "deleted_rows": deleted,
        "message": "本地行情数据已清空",
    }


@app.get("/api/report")
def report(
    sector_type: Literal["concept", "industry"] = settings.default_sector_type,  # type: ignore[assignment]
    report_date: str | None = Query(default=None),
    provider_name: ProviderName | None = Query(default=None, alias="provider"),
):
    return make_report(sector_type, report_date, provider_name)


@app.post("/api/sector-leaders")
def sector_leaders(request: SectorLeadersRequest):
    """Supplement selected board rows with best-effort constituent leaders."""
    active_provider = normalize_provider_name(request.provider)
    result = fetch_sector_leaders(
        [item.model_dump() for item in request.sectors],
        request.sector_type,
        active_provider,
    )
    return {
        "ok": True,
        "sector_type": request.sector_type,
        "provider": active_provider,
        "provider_label": MARKET_PROVIDER_LABELS[active_provider],
        **result,
    }


@app.post("/api/sector-constituents")
def sector_constituents(request: SectorConstituentsRequest):
    """Return the complete public constituent list for a selected board."""
    active_provider = normalize_provider_name(request.provider)
    if not request.sector_code.strip() and not request.sector_name.strip():
        raise HTTPException(status_code=400, detail="请先选择一个板块")
    try:
        result = fetch_sector_constituents(
            request.sector_code,
            request.sector_name,
            request.sector_type,
            active_provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"板块成分股读取失败：{type(exc).__name__}: {exc}",
        ) from exc
    return {"ok": True, "sector_type": request.sector_type, **result}


@app.get("/api/formulas")
def formulas():
    return {"ok": True, "formulas": database.list_formulas()}


@app.post("/api/formulas")
def save_formula_definition(request: FormulaDefinitionRequest):
    errors = validate_formula(request.formula)
    if errors:
        raise HTTPException(status_code=422, detail="公式无法保存：" + "；".join(errors))
    try:
        stored = database.save_formula(request.name, request.formula, request.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "formula": stored}


@app.delete("/api/formulas/{formula_id}")
def delete_formula_definition(formula_id: int):
    try:
        database.delete_formula(formula_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "formula_id": formula_id}


@app.get("/api/formula-runs")
def formula_runs(limit: int = Query(default=50, ge=1, le=200)):
    return {"ok": True, "runs": database.list_formula_runs(limit)}


@app.get("/api/formula-runs/{run_id}")
def formula_run(run_id: int):
    run = database.get_formula_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="选股结果不存在，可能已被删除")
    return {"ok": True, "run": run}


@app.delete("/api/formula-runs/{run_id}")
def delete_formula_run(run_id: int):
    if not database.delete_formula_run(run_id):
        raise HTTPException(status_code=404, detail="选股结果不存在，可能已被删除")
    return {"ok": True, "run_id": run_id}


@app.post("/api/formula-screen")
def formula_screen(request: FormulaScreenRequest):
    """Evaluate a saved TongDaXin formula and persist this screening run."""
    formula_record = None
    if request.formula_id is not None:
        formula_record = database.get_formula(request.formula_id)
        if formula_record is None:
            raise HTTPException(status_code=404, detail="选中的公式不存在，可能已被删除")
        formula_text = str(formula_record["formula"])
        formula_name = str(formula_record["name"])
    else:
        formula_text = str(request.formula or DEFAULT_FORMULA)
        formula_name = str(request.formula_name or "临时公式").strip() or "临时公式"
    validation_errors = validate_formula(formula_text)
    if validation_errors:
        raise HTTPException(status_code=422, detail="公式无法执行：" + "；".join(validation_errors))
    active_provider = normalize_provider_name(request.provider)
    try:
        result = screen_formula(formula_text, request.max_results, request.timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    timeframe_label = result.get("timeframe_label", request.timeframe)
    response = {
        "ok": True,
        "provider": "tdx_standard",
        "requested_provider": active_provider,
        "timeframe": request.timeframe,
        "timeframe_label": timeframe_label,
        "formula_id": formula_record["id"] if formula_record else None,
        "formula_name": formula_name,
        "formula_saved": formula_record is not None,
        **result,
    }
    run_id = database.save_formula_run(
        {
            "formula_id": formula_record["id"] if formula_record else None,
            "formula_name": formula_name,
            "formula": formula_text,
            "timeframe": request.timeframe,
            "provider": active_provider,
            "result": response,
        }
    )
    response["run_id"] = run_id
    response["saved_at"] = database.get_formula_run(run_id)["created_at"]
    return response


@app.post("/api/llm-report")
def llm_report(request: LLMRequest):
    report_data = make_report(
        request.sector_type, request.report_date, request.provider
    )
    selected_model = normalize_llm_model(request.model)
    client = ArkClient(
        llm_api_key_for_model(selected_model),
        settings.ark_base_url,
        selected_model,
        settings.ark_timeout_seconds,
        settings.ark_use_env_proxy,
        settings.ark_thinking_type,
        settings.ark_max_tokens,
        llm_key_hint_for_model(selected_model),
    )
    try:
        text = client.generate_review(report_data["llm_input"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "report_date": report_data["report_date"],
        "sector_type": report_data["sector_type"],
        "model": selected_model,
        "model_label": llm_model_label(selected_model),
        "provider": normalize_provider_name(request.provider),
        "content": text,
        "finish_reason": client.last_finish_reason,
        "truncated": client.last_finish_reason == "length",
    }


@app.get("/api/quote")
def quote(
    symbols: str = Query(default=""),
    provider_name: ProviderName | None = Query(default=None, alias="provider"),
):
    items = [item.strip() for item in re.split(r"[\s,，]+", symbols.strip()) if item.strip()]
    if not items:
        raise HTTPException(status_code=400, detail="请通过 symbols 参数传入股票代码，例如 000001,600000")
    if len(items) > 100:
        raise HTTPException(status_code=400, detail="一次最多查询 100 个代码")
    source = provider(provider_name)
    try:
        rows = source.realtime_quotes(items)
        return {
            "ok": True,
            "provider": source.name,
            "provider_label": MARKET_PROVIDER_LABELS.get(source.name, source.name),
            "quotes": [normalize_quote_row(row) for row in rows],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"实时行情读取失败：{type(exc).__name__}: {exc}") from exc
    finally:
        close_provider(source)


_flow_operation_lock = Lock()
_flow_active_operations = 0
_flow_jobs = FlowHistoryJobs()
_daily_collector = None


@app.on_event("startup")
def resume_flow_updates():
    global _daily_collector
    _flow_jobs.start_recovery(database, flow_operation)
    if settings.auto_flow_archive:
        _daily_collector = DailyCollector(database,flow_operation)
        _daily_collector.start()


@app.on_event("shutdown")
def pause_flow_updates():
    if _daily_collector: _daily_collector.shutdown()
    _flow_jobs.shutdown()


class FlowImportRequest(BaseModel):
    source: Literal["tdx_import", "ths_import"]
    sector_type: Literal["concept", "industry"]
    content: str = Field(min_length=1, max_length=6_000_000)
    unit: Literal["元", "万元", "亿元"] = "元"
    overwrite: bool = False


class FlowHistoryJobRequest(BaseModel):
    sector_type: Literal["concept", "industry"] = "concept"
    date: str = Field(min_length=10, max_length=10)
    mode: Literal["daily","history"] = "daily"
    window_days: int = Field(default=10, ge=1, le=60, strict=True)
    min_inflow_days: int = Field(default=6, ge=1, le=60, strict=True)


def flow_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        global _flow_active_operations
        with _flow_operation_lock:
            _flow_active_operations += 1
        try:
            return function(*args, **kwargs)
        finally:
            with _flow_operation_lock:
                _flow_active_operations -= 1
    return wrapped


@app.post("/api/sector-capital-flow/clear")
def clear_capital_flow():
    with _flow_operation_lock:
        if _flow_active_operations or _flow_jobs.is_running():
            raise HTTPException(status_code=409, detail="资金流向正在查询，请等待完成后再清空")
        try:
            result = database.clear_capital_flow_data()
        except RuntimeError as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from exc
    return {"ok": True, **result, "message": "资金流向数据已清空，其他数据未改动"}


@app.post("/api/sector-capital-flow/clear-cache")
def clear_flow_cache():
    with _flow_operation_lock:
        if _flow_active_operations or _flow_jobs.is_running():
            raise HTTPException(status_code=409,detail="资金任务正在进行，请完成后清理缓存")
        try:
            result = database.clear_capital_flow_data(cache_only=True)
        except RuntimeError as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from exc
    return {"ok":True,**result,"message":"已清理当前快照和显示缓存；逐日历史、修订记录、导入记录均保留"}


@app.get("/api/sector-capital-flow/archive-status")
def flow_archive_status():
    with database.connection() as conn:
        rows = conn.execute("SELECT sector_type,trade_date,COUNT(*) AS boards FROM sector_capital_flow_daily GROUP BY sector_type,trade_date ORDER BY trade_date DESC LIMIT 40").fetchall()
        runs = conn.execute("SELECT sector_type,trade_date,state_json FROM capital_flow_collection ORDER BY trade_date DESC LIMIT 4").fetchall()
    return {"auto_archive":settings.auto_flow_archive,"feed_configured":bool(settings.flow_feed_urls),
            "days":[dict(row) for row in rows],"collection":[{"sector_type":row[0],"trade_date":row[1],**json.loads(row[2])} for row in runs],
            "sources":{"eastmoney":"可联网查看当天榜并归档", "ths_online":"未启用：实测HTTP403，尚未通过联网验证", "tdx_online":"未启用：普通日线文件不含主力净流入"}}


@app.get("/api/sector-capital-flow/import-template")
def flow_import_template():
    return Response("\ufeff" + TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="capital-flow-template.csv"'})


@app.post("/api/sector-capital-flow/import")
@flow_operation
def import_capital_flow(request: FlowImportRequest):
    try:
        return import_flow(database, request.source, request.content, request.sector_type, request.unit, request.overwrite)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sector-capital-flow/history/jobs")
def start_flow_history_job(request: FlowHistoryJobRequest):
    try:
        with _flow_operation_lock:
            return _flow_jobs.start(database, request.sector_type, request.date, flow_operation,mode=request.mode,feed_urls=settings.flow_feed_urls,
                                   window_days=request.window_days,min_inflow_days=request.min_inflow_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sector-capital-flow/history/jobs")
def pending_flow_history_jobs():
    return {"jobs":database.pending_flow_jobs()}


@app.get("/api/sector-capital-flow/history/jobs/{job_id}")
def read_flow_history_job(job_id: str):
    try:
        job = _flow_jobs.snapshot(job_id,database)
        report = flow_preview(job.get("result"), job["sector_type"], job["date"], 1000,
                              window_days=job.get("window_days",10),min_inflow_days=job.get("min_inflow_days",6))
        if report:
            job["result"] = report
        return job
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务已结束或软件已重启；成功下载的数据仍保存在本地") from exc


@app.post("/api/sector-capital-flow/history/jobs/{job_id}/cancel")
def cancel_flow_history_job(job_id: str):
    try:
        return _flow_jobs.cancel(job_id,database)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="找不到补齐任务") from exc


def flow_preview(report, sector_type, selected_date, limit, *, window_days=10, min_inflow_days=6):
    """Expose an explicit preview without storing it as closing history."""
    result = dict(report or {})
    result["source_id"] = "eastmoney"
    now = shanghai_now()
    if selected_date != now.date().isoformat() or (now.hour, now.minute) >= (15, 5) or is_trading_day(now.date()) is not True:
        return result if report else None
    current = database.get_current_capital_flow(sector_type) or {}
    rows = []
    for row in current.get("rows") or []:
        try:
            stamp = datetime.fromisoformat(str(row.get("source_time")))
            if stamp.tzinfo is not None and stamp.astimezone(SHANGHAI).date() == now.date() and stamp <= now:
                rows.append(row)
        except (ValueError, TypeError):
            continue
    if not rows:
        return result if report else None
    if not report:
        closed, _ = latest_closed_trading_day(now.date())
        result.update(ok=True, sector_type=sector_type, requested_date=selected_date,
                      resolved_date=closed.isoformat(), report_date=None, rows=[], total=0,
                      window_dates=[], window_days=window_days, min_inflow_days=min_inflow_days, window_complete=False, daily_complete=False, partial=True,
                      date_confirmed=False, catalog_complete=False, inflow_days_rank=[], cached=True,
                      source="东方财富本地盘中快照", history_coverage={"requested_boards": len(rows), "complete_boards": 0, "window_days": 0},
                      warnings=["今日尚未收盘；可以查看今日预览，收盘历史尚无记录，需要另行补齐。"])
    result["preview"] = {"date": selected_date, "rows": sorted(rows, key=lambda row: row.get("main_net_inflow") or 0, reverse=True)[:limit],
                         "total": len(rows), "updated_at": current.get("updated_at"),
                         "note": f"今日盘中预览，来自已保存的当前快照，不是收盘数据，不参与{window_days}日资金流入榜。"}
    return result


@app.get("/api/sector-capital-flow/current/cache")
@flow_operation
def cached_current_capital_flow(
    sector_type: Literal["concept", "industry"] = "concept",
    limit: int = Query(default=50, ge=1, le=1000),
):
    saved = database.get_current_capital_flow(sector_type)
    if saved is None:
        raise HTTPException(status_code=404, detail="本机还没有保存过当前资金流向，请点击刷新当前资金流向")
    saved = share_current_flow_with_history(saved, database)
    return present_sector_capital_flow_report(saved, limit, cached=True)


@app.get("/api/sector-capital-flow/current")
@flow_operation
def current_capital_flow(
    sector_type: Literal["concept", "industry"] = "concept",
    limit: int = Query(default=50, ge=1, le=1000),
):
    # Current quotes must never depend on the historical endpoint or date picker.
    try:
        result = fetch_sector_capital_flow(sector_type, 1000)
        result["kind"] = "current"
        result = share_current_flow_with_history(result, database)
        database.save_current_capital_flow(result)
        database.save_sector_capital_flow_catalog(sector_type, result["rows"])
        return present_sector_capital_flow_report(result, limit, cached=False)
    except Exception as exc:
        saved = database.get_current_capital_flow(sector_type)
        if saved:
            result = present_sector_capital_flow_report(saved, limit, cached=True)
            result["refresh_error"] = str(exc)
            return result
        raise HTTPException(status_code=502, detail=f"当前资金流向读取失败：{exc}") from exc


@app.get("/api/sector-capital-flow")
@flow_operation
def sector_capital_flow(
    sector_type: Literal["concept", "industry"] = settings.default_sector_type,  # type: ignore[assignment]
    limit: int = Query(default=50, ge=1, le=1000),
    report_date: str | None = Query(default=None, alias="date"),
    refresh: bool = Query(default=False),
    retry_missing: bool = Query(default=False),
    source: Literal["eastmoney", "tdx_import", "ths_import"] = "eastmoney",
    window_days: int = Query(default=10, ge=1, le=60),
    min_inflow_days: int = Query(default=6, ge=1, le=60),
):
    """Return a saved snapshot first; fetch from Eastmoney only if needed."""
    selected_date = report_date or shanghai_now().date().isoformat()
    try:
        validate_flow_rule(window_days,min_inflow_days)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    if source != "eastmoney":
        try:
            return imported_flow_report(database, source, sector_type, selected_date, limit,
                                        window_days=window_days,min_inflow_days=min_inflow_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail="此来源、类型和日期暂无可用导入数据；请先导入逐日板块资金文件") from exc
    saved = database.get_sector_capital_flow_report(sector_type, selected_date)
    if saved:
        saved = flow_report_with_rule(saved,window_days,min_inflow_days)
    try:
        if not refresh and not retry_missing:
            try:
                # Rebuild from shared daily rows instead of returning a stale
                # report snapshot, and never wait for 10-day backfill here.
                return flow_preview(fetch_sector_capital_flow_report(
                    sector_type, limit, selected_date, database, local_only=True,
                    window_days=window_days,min_inflow_days=min_inflow_days), sector_type, selected_date, limit,
                    window_days=window_days,min_inflow_days=min_inflow_days)
            except RuntimeError:
                if saved:
                    return flow_preview(present_sector_capital_flow_report(saved, limit, cached=True), sector_type, selected_date, limit,
                                        window_days=window_days,min_inflow_days=min_inflow_days)
                preview = flow_preview(None, sector_type, selected_date, limit,window_days=window_days,min_inflow_days=min_inflow_days)
                if preview:
                    return preview
                old_report = database.get_sector_capital_flow_report(sector_type,max_date=selected_date)
                if old_report:
                    result = present_sector_capital_flow_report(flow_report_with_rule(old_report,window_days,min_inflow_days),limit,cached=True)
                    result.update(requested_date=selected_date,date_confirmed=False,partial=True,
                                  refresh_error="所选日期没有档案，正在展示已有旧记录；未联网")
                    return result
                raise HTTPException(status_code=404,detail="本机没有该日期的档案；请点击一键更新，或在高级功能尝试历史补缺")
        return flow_preview(fetch_sector_capital_flow_report(
            sector_type, limit, selected_date, database, refresh=refresh and not retry_missing,
            retry_missing=retry_missing, daily_only=not retry_missing,
            window_days=window_days,min_inflow_days=min_inflow_days,
        ), sector_type, selected_date, limit,window_days=window_days,min_inflow_days=min_inflow_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        if not saved:
            saved = database.get_sector_capital_flow_report(sector_type, max_date=selected_date)
        if saved:
            result = present_sector_capital_flow_report(flow_report_with_rule(saved,window_days,min_inflow_days), limit, cached=True)
            result["cached_requested_date"] = result.get("requested_date")
            result["requested_date"] = selected_date
            result["date_confirmed"] = False
            result["partial"] = True
            result["refresh_error"] = f"{type(exc).__name__}: {exc}"
            return flow_preview(result, sector_type, selected_date, limit,window_days=window_days,min_inflow_days=min_inflow_days)
        raise HTTPException(
            status_code=502,
            detail=f"东方财富板块资金流向读取失败：{type(exc).__name__}: {exc}",
        ) from exc


@app.get("/api/sector-capital-flow/cache")
@flow_operation
def cached_sector_capital_flow(
    sector_type: Literal["concept", "industry"] = settings.default_sector_type,  # type: ignore[assignment]
    limit: int = Query(default=50, ge=1, le=1000),
    report_date: str | None = Query(default=None, alias="date"),
    source: Literal["eastmoney", "tdx_import", "ths_import"] = "eastmoney",
    window_days: int = Query(default=10, ge=1, le=60),
    min_inflow_days: int = Query(default=6, ge=1, le=60),
):
    """Restore the most recent saved flow result without making network calls."""
    try:
        validate_flow_rule(window_days,min_inflow_days)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    if source != "eastmoney":
        try:
            return imported_flow_report(database, source, sector_type, report_date, limit,
                                        window_days=window_days,min_inflow_days=min_inflow_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail="此来源暂无可用导入数据，请先导入") from exc
    saved = database.get_sector_capital_flow_report(sector_type, report_date)
    try:
        return flow_preview(fetch_sector_capital_flow_report(
            sector_type, limit, report_date, database, local_only=True,window_days=window_days,min_inflow_days=min_inflow_days), sector_type,
            report_date or shanghai_now().date().isoformat(), limit,window_days=window_days,min_inflow_days=min_inflow_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if saved is None:
            preview = flow_preview(None, sector_type, report_date or shanghai_now().date().isoformat(), limit,
                                   window_days=window_days,min_inflow_days=min_inflow_days)
            if preview:
                return preview
            raise HTTPException(status_code=404, detail="本机还没有保存过这个日期的资金流向结果") from exc
    return flow_preview(present_sector_capital_flow_report(flow_report_with_rule(saved,window_days,min_inflow_days), limit, cached=True), sector_type,
                        report_date or shanghai_now().date().isoformat(), limit,window_days=window_days,min_inflow_days=min_inflow_days)


def normalize_quote_row(row: dict) -> dict:
    """Expose stable API keys while retaining the upstream raw fields."""
    return {
        **row,
        "symbol": _first_quote_value(row, "代码", "股票代码", "symbol", "code"),
        "name": _first_quote_value(row, "名称", "股票名称", "name"),
        "price": _first_quote_value(row, "最新价", "现价", "price"),
        "prev_close": _first_quote_value(row, "昨收", "昨日收盘", "prev_close"),
        "open": _first_quote_value(row, "今开", "开盘", "open"),
        "high": _first_quote_value(row, "最高", "high"),
        "low": _first_quote_value(row, "最低", "low"),
        "change": _first_quote_value(row, "涨跌额", "change"),
        "pct_change": _first_quote_value(row, "涨跌幅", "涨跌幅(%)", "pct_change"),
        "volume": _first_quote_value(row, "成交量", "volume"),
        "amount": _first_quote_value(row, "成交额", "成交金额", "amount"),
        "data_source": _first_quote_value(row, "数据源", "data_source", "source"),
    }


def _first_quote_value(row: dict, *keys: str):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None
