from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _runtime_root() -> Path:
    """Return the project root for both source and packaged desktop builds."""
    if getattr(sys, "frozen", False):
        # PyInstaller stores Python modules under its internal directory.  User
        # data and the editable .env should live next to the executable so the
        # packaged app can be moved to another Windows computer as a folder.
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT_DIR = _runtime_root()
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
ENV_FILE = ROOT_DIR / ".env"
if not ENV_FILE.exists():
    ENV_FILE = BUNDLE_DIR / ".env"
load_dotenv(ENV_FILE)


DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro-ga-260813"
DEFAULT_DOUBAO_MODEL = "doubao-seed-2-1-pro-260628"


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _choice_env(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name, "").strip().lower()
    return value if value in choices else default


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT_DIR
    asset_dir: Path = BUNDLE_DIR
    ark_api_key: str = os.getenv("ARK_API_KEY", "").strip()
    # Optional key dedicated to Doubao.  When empty, Doubao intentionally
    # falls back to ARK_API_KEY for backwards compatibility.
    ark_doubao_api_key: str = os.getenv("ARK_DOUBAO_API_KEY", "").strip()
    ark_base_url: str = os.getenv(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    ).rstrip("/")
    ark_model: str = os.getenv("ARK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip()
    ark_doubao_model: str = os.getenv(
        "ARK_DOUBAO_MODEL", DEFAULT_DOUBAO_MODEL
    ).strip()
    market_provider: str = os.getenv("MARKET_PROVIDER", "akshare").strip().lower()
    default_sector_type: str = os.getenv(
        "DEFAULT_SECTOR_TYPE", "concept"
    ).strip().lower()
    database_path: Path = ROOT_DIR / os.getenv(
        "DATABASE_PATH", "data/gupiao.db"
    ).strip()
    report_dir: Path = ROOT_DIR / os.getenv("REPORT_DIR", "reports").strip()
    rps_window: int = _int_env("RPS_WINDOW", 50)
    akshare_workers: int = max(1, _int_env("AKSHARE_WORKERS", 4))
    market_catalog_timeout_seconds: float = max(
        30.0,
        min(180.0, _float_env("MARKET_CATALOG_TIMEOUT_SECONDS", 120.0)),
    )
    # ``auto`` uses requests' environment or system proxy when one exists and
    # otherwise connects directly. ``env``/``direct`` can force either path.
    market_http_mode: str = _choice_env(
        "MARKET_HTTP_MODE", "auto", {"auto", "env", "direct"}
    )
    ark_timeout_seconds: float = max(
        10.0, min(180.0, _float_env("ARK_TIMEOUT_SECONDS", 180.0))
    )
    ark_use_env_proxy: bool = _bool_env("ARK_USE_ENV_PROXY", False)
    ark_thinking_type: str = _choice_env(
        "ARK_THINKING_TYPE", "disabled", {"enabled", "disabled", "auto"}
    )
    ark_max_tokens: int = max(512, min(4096, _int_env("ARK_MAX_TOKENS", 4096)))
    tdx_root: str = os.getenv("TDX_ROOT", "").strip()
    tdx_servers: str = os.getenv("TDX_SERVERS", "").strip()
    # Optional refresh of the selected market data after the page is opened.
    # Keep it disabled by default so startup is not delayed by network calls.
    auto_sync_on_open: bool = _bool_env("AUTO_SYNC_ON_OPEN", False)
    auto_close_sync: bool = _bool_env("AUTO_CLOSE_SYNC", False)
    auto_close_sync_time: str = os.getenv("AUTO_CLOSE_SYNC_TIME", "15:05").strip() or "15:05"
    auto_close_sync_types: str = os.getenv(
        "AUTO_CLOSE_SYNC_TYPES", "concept,industry"
    ).strip()
    # Independent of market/report auto sync; no imports or paid services needed.
    auto_flow_archive: bool = _bool_env("AUTO_FLOW_ARCHIVE", True)
    # Publisher-only opt-in. Empty until source redistribution is authorized.
    flow_feed_urls: tuple[str, ...] = tuple(url.strip() for url in os.getenv("FLOW_FEED_URLS", "").split(",") if url.strip())
    # Optional public HTTPS JSON manifest used by the desktop updater.  Keep
    # this empty in the source template until the first GitHub Release exists.
    update_manifest_url: str = os.getenv("UPDATE_MANIFEST_URL", "").strip() or "https://raw.githubusercontent.com/yefeng1346/gupiao-updates/main/latest.json"
    update_check_on_startup: bool = _bool_env("UPDATE_CHECK_ON_STARTUP", True)


settings = Settings()


def ensure_directories() -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.report_dir.mkdir(parents=True, exist_ok=True)
