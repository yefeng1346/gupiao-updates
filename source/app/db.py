from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dataset_id(data_source: str) -> str:
    value = str(data_source or "").strip().lower()
    if value == "tdx_local":
        return "tdx_standard"
    if value == "tdx_protocol":
        return "tdx_online"
    if value in {"tdx", "tdx_standard", "tdx_online", "akshare", "efinance"}:
        return value
    if value.startswith("snapshot") or value == "import":
        return "snapshot"
    return "legacy"


class Database:
    """SQLite persistence for normalized board data and sync history.

    The two ``source_*`` columns keep optional values supplied by an upstream
    snapshot API.  Normal market-history providers do not usually expose the
    original board rank/RPS, so the analytics layer calculates those values
    when the source values are absent.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sector_daily (
                    trade_date TEXT NOT NULL,
                    sector_type TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    close REAL,
                    pct_change REAL,
                    amount REAL,
                    volume REAL,
                    rps50 REAL,
                    rank INTEGER,
                    source_rank INTEGER,
                    source_rps50 REAL,
                    data_source TEXT,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, sector_type, sector_code)
                );

                CREATE INDEX IF NOT EXISTS idx_sector_daily_date
                    ON sector_daily (sector_type, trade_date);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    sector_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    requested_sectors INTEGER NOT NULL,
                    succeeded_sectors INTEGER NOT NULL,
                    saved_rows INTEGER NOT NULL,
                    errors_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sector_catalog (
                    sector_type TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    code_system TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (sector_type, sector_code)
                );

                CREATE INDEX IF NOT EXISTS idx_sector_catalog_name
                    ON sector_catalog (sector_type, sector_name);

                CREATE TABLE IF NOT EXISTS sector_capital_flow_daily (
                    sector_type TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    latest_price REAL,
                    pct_change REAL,
                    main_net_inflow REAL,
                    main_net_ratio REAL,
                    super_large_net_inflow REAL,
                    super_large_ratio REAL,
                    large_net_inflow REAL,
                    large_ratio REAL,
                    medium_net_inflow REAL,
                    medium_ratio REAL,
                    small_net_inflow REAL,
                    small_ratio REAL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (sector_type, trade_date, sector_code)
                );

                CREATE TABLE IF NOT EXISTS capital_flow_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_type TEXT NOT NULL, trade_date TEXT NOT NULL,
                    sector_code TEXT NOT NULL, previous_json TEXT NOT NULL,
                    revised_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capital_flow_collection (
                    sector_type TEXT NOT NULL, trade_date TEXT NOT NULL,
                    state_json TEXT NOT NULL, owner TEXT NOT NULL,
                    lease_until REAL NOT NULL, next_attempt REAL NOT NULL,
                    PRIMARY KEY(sector_type, trade_date)
                );

                CREATE INDEX IF NOT EXISTS idx_sector_capital_flow_history
                    ON sector_capital_flow_daily (sector_type, trade_date, sector_code);

                CREATE TABLE IF NOT EXISTS sector_capital_flow_reports (
                    sector_type TEXT NOT NULL,
                    requested_date TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    PRIMARY KEY (sector_type, requested_date)
                );

                CREATE INDEX IF NOT EXISTS idx_sector_capital_flow_reports_saved
                    ON sector_capital_flow_reports (sector_type, saved_at DESC);

                CREATE TABLE IF NOT EXISTS sector_capital_flow_catalog (
                    sector_type TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (sector_type, sector_code)
                );

                CREATE TABLE IF NOT EXISTS sector_capital_flow_current (
                    sector_type TEXT PRIMARY KEY,
                    report_json TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS imported_capital_flow_daily (
                    source TEXT NOT NULL,
                    sector_type TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY (source, sector_type, trade_date, sector_code)
                );

                CREATE TABLE IF NOT EXISTS capital_flow_jobs (
                    id TEXT PRIMARY KEY,
                    sector_type TEXT NOT NULL,
                    selected_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS formula_definitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    formula TEXT NOT NULL,
                    is_builtin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS formula_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    formula_id INTEGER,
                    formula_name TEXT NOT NULL,
                    formula TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    technical_candidates INTEGER NOT NULL DEFAULT 0,
                    fundamental_verified INTEGER NOT NULL DEFAULT 0,
                    warnings_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_formula_runs_created
                    ON formula_runs (created_at DESC);

                CREATE TABLE IF NOT EXISTS candidate_runs (
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL,
                    provider TEXT NOT NULL, sector_type TEXT NOT NULL,
                    state_json TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_runs_latest
                    ON candidate_runs(provider,sector_type,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_candidate_runs_request
                    ON candidate_runs(request_key,created_at DESC);
                CREATE TABLE IF NOT EXISTS candidate_ai_cache (
                    cache_key TEXT PRIMARY KEY, content_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_ai_leases (
                    cache_key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL
                );
                """
            )
            # Existing installations were created before the source columns
            # existed.  ALTER TABLE keeps all historical rows intact.
            existing = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(sector_daily)")
            }
            migrations = {
                "source_rank": "INTEGER",
                "source_rps50": "REAL",
                "data_source": "TEXT",
            }
            for column, kind in migrations.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE sector_daily ADD COLUMN {column} {kind}")
            self._migrate_dataset_schema(conn)

    def upsert_sector_capital_flow_daily(self, rows: Iterable[dict[str, Any]]) -> int:
        values = []
        fetched_at = _now()
        columns = (
            "latest_price",
            "pct_change",
            "main_net_inflow",
            "main_net_ratio",
            "super_large_net_inflow",
            "super_large_ratio",
            "large_net_inflow",
            "large_ratio",
            "medium_net_inflow",
            "medium_ratio",
            "small_net_inflow",
            "small_ratio",
        )
        for row in rows:
            sector_type = str(row.get("sector_type") or "").strip()
            trade_date = str(row.get("trade_date") or "").strip()
            sector_code = str(row.get("sector_code") or "").strip()
            sector_name = str(row.get("sector_name") or "").strip()
            if not all((sector_type, trade_date, sector_code, sector_name)):
                continue
            values.append(
                (
                    sector_type,
                    trade_date,
                    sector_code,
                    sector_name,
                    *(_number(row.get(column)) for column in columns),
                    fetched_at,
                )
            )
        if not values:
            return 0
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for value in values:
                old = conn.execute("SELECT * FROM sector_capital_flow_daily WHERE sector_type=? AND trade_date=? AND sector_code=?",value[:3]).fetchone()
                if old and (old["sector_name"] != value[3] or any(value[index+4] is not None and old[column] != value[index+4] for index,column in enumerate(columns))):
                    conn.execute("INSERT INTO capital_flow_revisions(sector_type,trade_date,sector_code,previous_json,revised_at) VALUES(?,?,?,?,?)",(*value[:3],json.dumps(dict(old),ensure_ascii=False),fetched_at))
            conn.executemany(
                """
                INSERT INTO sector_capital_flow_daily (
                    sector_type, trade_date, sector_code, sector_name,
                    latest_price, pct_change, main_net_inflow, main_net_ratio,
                    super_large_net_inflow, super_large_ratio,
                    large_net_inflow, large_ratio, medium_net_inflow, medium_ratio,
                    small_net_inflow, small_ratio, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sector_type, trade_date, sector_code) DO UPDATE SET
                    sector_name=excluded.sector_name,
                    latest_price=COALESCE(excluded.latest_price, sector_capital_flow_daily.latest_price),
                    pct_change=COALESCE(excluded.pct_change, sector_capital_flow_daily.pct_change),
                    main_net_inflow=COALESCE(excluded.main_net_inflow, sector_capital_flow_daily.main_net_inflow),
                    main_net_ratio=COALESCE(excluded.main_net_ratio, sector_capital_flow_daily.main_net_ratio),
                    super_large_net_inflow=COALESCE(excluded.super_large_net_inflow, sector_capital_flow_daily.super_large_net_inflow),
                    super_large_ratio=COALESCE(excluded.super_large_ratio, sector_capital_flow_daily.super_large_ratio),
                    large_net_inflow=COALESCE(excluded.large_net_inflow, sector_capital_flow_daily.large_net_inflow),
                    large_ratio=COALESCE(excluded.large_ratio, sector_capital_flow_daily.large_ratio),
                    medium_net_inflow=COALESCE(excluded.medium_net_inflow, sector_capital_flow_daily.medium_net_inflow),
                    medium_ratio=COALESCE(excluded.medium_ratio, sector_capital_flow_daily.medium_ratio),
                    small_net_inflow=COALESCE(excluded.small_net_inflow, sector_capital_flow_daily.small_net_inflow),
                    small_ratio=COALESCE(excluded.small_ratio, sector_capital_flow_daily.small_ratio),
                    fetched_at=excluded.fetched_at
                """,
                values,
            )
        return len(values)

    def get_flow_job(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM capital_flow_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {**json.loads(row["state_json"]), "cancel_requested": bool(row["cancel_requested"])}

    def pending_flow_jobs(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT state_json FROM capital_flow_jobs WHERE status IN ('running','paused') ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _guard_flow_jobs(conn):
        import time
        if conn.execute("SELECT 1 FROM capital_flow_jobs WHERE status='running' AND lease_until>? LIMIT 1",(time.time(),)).fetchone():
            raise RuntimeError("另一个窗口可能正在更新资金数据，请先完成或取消任务再清空")
        if conn.execute("SELECT 1 FROM capital_flow_collection WHERE lease_until>? LIMIT 1",(time.time(),)).fetchone():
            raise RuntimeError("后台收盘归档正在进行，请稍后清理")

    def claim_flow_job(self, state, owner, *, restart_expired=False):
        """Atomic process lease prevents two open EXEs duplicating one task."""
        import time
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM capital_flow_jobs WHERE sector_type=? AND selected_date=? AND status IN ('running','paused') ORDER BY rowid DESC LIMIT 1",
                               (state["sector_type"],state["date"])).fetchone()
            if row:
                saved = {**json.loads(row["state_json"]), "cancel_requested":bool(row["cancel_requested"])}
                if row["lease_until"] > now and row["owner"] != owner:
                    return saved, False
                if restart_expired and row["lease_until"] <= now and (saved.get("expires_at",0) <= now or saved.get("round",0) >= saved.get("max_rounds",3)):
                    saved.update(status="partial",next_retry_at=0,message="旧任务已到期；用户重新点击后创建新任务，已保存数据保留")
                    conn.execute("UPDATE capital_flow_jobs SET status='partial',state_json=?,lease_until=0 WHERE id=?",
                                 (json.dumps(saved,ensure_ascii=False),saved["id"]))
                    row = None
                else:
                    if (saved.get("window_days",10),saved.get("min_inflow_days",6)) != (state.get("window_days",10),state.get("min_inflow_days",6)):
                        raise ValueError("该日期已有另一组条件的未完成任务，请完成或取消后调整条件")
                    state = saved
            if row:
                state["status"] = "running"
                conn.execute("UPDATE capital_flow_jobs SET owner=?,lease_until=?,status='running',state_json=? WHERE id=?",
                             (owner,now+120,json.dumps(state,ensure_ascii=False),state["id"]))
            else:
                active = conn.execute("SELECT COUNT(*) FROM capital_flow_jobs WHERE status IN ('running','paused') AND lease_until > ?",(now,)).fetchone()[0]
                if active >= 2:
                    raise ValueError("已有两个资金更新任务，请完成或取消后再启动")
                conn.execute("INSERT INTO capital_flow_jobs VALUES (?,?,?,?,?,?,?,0)",
                             (state["id"],state["sector_type"],state["date"],state["status"],json.dumps(state,ensure_ascii=False),owner,now+120))
            # Retain bounded diagnostic metadata, not keys or complete datasets.
            conn.execute("DELETE FROM capital_flow_jobs WHERE status NOT IN ('running','paused') AND id NOT IN (SELECT id FROM capital_flow_jobs ORDER BY rowid DESC LIMIT 20)")
        return state, True

    def save_flow_job(self, state, owner):
        import time
        # Result rows already have their own tables. Keep checkpoints small.
        state = {k:v for k,v in state.items() if k not in {"result","cancel_requested"}}
        with self.connection() as conn:
            cursor = conn.execute("UPDATE capital_flow_jobs SET status=?, state_json=?,lease_until=? WHERE id=? AND owner=? AND (status IN ('running','paused') OR ? != 'running')",
                        (state["status"],json.dumps(state,ensure_ascii=False),time.time()+120 if state["status"]=="running" else 0,state["id"],owner,state["status"]))
        return cursor.rowcount == 1

    def cancel_flow_job(self, job_id):
        with self.connection() as conn:
            cursor = conn.execute("UPDATE capital_flow_jobs SET cancel_requested=1 WHERE id=?",(job_id,))
        return cursor.rowcount == 1

    def get_sector_capital_flow_daily(
        self,
        sector_type: str,
        sector_codes: Iterable[str],
        trade_dates: Iterable[str],
    ) -> list[dict[str, Any]]:
        codes = list(dict.fromkeys(str(code).strip() for code in sector_codes if str(code).strip()))
        dates = list(dict.fromkeys(str(day).strip() for day in trade_dates if str(day).strip()))
        if not codes or not dates:
            return []
        code_marks = ",".join("?" for _ in codes)
        date_marks = ",".join("?" for _ in dates)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT sector_type, trade_date, sector_code, sector_name,
                       latest_price, pct_change, main_net_inflow, main_net_ratio,
                       super_large_net_inflow, super_large_ratio,
                       large_net_inflow, large_ratio, medium_net_inflow,
                       medium_ratio, small_net_inflow, small_ratio
                  FROM sector_capital_flow_daily
                 WHERE sector_type = ?
                   AND sector_code IN ({code_marks})
                   AND trade_date IN ({date_marks})
                """,
                [sector_type, *codes, *dates],
            ).fetchall()
        return [dict(row) for row in rows]

    def save_sector_capital_flow_report(self, report: dict[str, Any]) -> None:
        sector_type = str(report.get("sector_type") or "").strip()
        requested_date = str(report.get("requested_date") or "").strip()
        if sector_type not in {"concept", "industry"} or not requested_date:
            raise ValueError("资金流向报告缺少有效的板块类型或日期")
        report_json = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sector_capital_flow_reports (
                    sector_type, requested_date, report_json, saved_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(sector_type, requested_date) DO UPDATE SET
                    report_json=excluded.report_json,
                    saved_at=excluded.saved_at
                """,
                (sector_type, requested_date, report_json, _now()),
            )

    def save_current_capital_flow(self, report: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sector_capital_flow_current VALUES (?, ?, ?)",
                (report["sector_type"], json.dumps(report, ensure_ascii=False), datetime.now().isoformat()),
            )

    def get_current_capital_flow(self, sector_type: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT report_json, saved_at FROM sector_capital_flow_current WHERE sector_type = ?", (sector_type,)).fetchone()
        if row is None:
            return None
        result = json.loads(row["report_json"])
        result["saved_at"] = row["saved_at"]
        return result

    def save_sector_capital_flow_catalog(self, sector_type: str, boards: Iterable[dict]) -> None:
        values = [
            (sector_type, str(board["sector_code"]), str(board["sector_name"]), _now())
            for board in boards if board.get("sector_code") and board.get("sector_name")
        ]
        if not values:
            return
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO sector_capital_flow_catalog VALUES (?, ?, ?, ?)
                ON CONFLICT(sector_type, sector_code) DO UPDATE SET
                    sector_name=excluded.sector_name, updated_at=excluded.updated_at
                """,
                values,
            )

    def get_sector_capital_flow_catalog(self, sector_type: str) -> list[dict[str, Any]]:
        derived = False
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT sector_code, sector_name FROM sector_capital_flow_catalog WHERE sector_type = ?",
                (sector_type,),
            ).fetchall()
            if not rows:
                derived = True
                # Upgrade older installations without requiring another directory download.
                rows = conn.execute(
                    """
                    SELECT sector_code, sector_name FROM (
                        SELECT sector_code, sector_name,
                               ROW_NUMBER() OVER (PARTITION BY sector_code ORDER BY trade_date DESC) AS rn
                          FROM sector_capital_flow_daily WHERE sector_type = ?
                    ) WHERE rn = 1
                    """,
                    (sector_type,),
                ).fetchall()
        return [{**dict(row), "catalog_derived": derived} for row in rows]

    def get_sector_capital_flow_history(
        self, sector_type: str, report_date: str, window_days: int = 10
    ) -> list[dict[str, Any]]:
        """Read the most recent stored days at/before a cutoff, without networking."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sector_capital_flow_daily
                 WHERE sector_type = ? AND trade_date IN (
                    SELECT DISTINCT trade_date FROM sector_capital_flow_daily
                     WHERE sector_type = ? AND trade_date <= ? AND main_net_inflow IS NOT NULL
                     ORDER BY trade_date DESC LIMIT ?
                 ) ORDER BY trade_date, sector_code
                """,
                (sector_type, sector_type, report_date, max(1, min(int(window_days), 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_sector_capital_flow_report(
        self, sector_type: str, requested_date: str | None = None, *, max_date: str | None = None
    ) -> dict[str, Any] | None:
        if sector_type not in {"concept", "industry"}:
            return None
        with self.connection() as conn:
            if requested_date:
                row = conn.execute(
                    """
                    SELECT report_json, saved_at
                      FROM sector_capital_flow_reports
                     WHERE sector_type = ? AND requested_date = ?
                    """,
                    (sector_type, requested_date),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT report_json, saved_at
                      FROM sector_capital_flow_reports
                     WHERE sector_type = ?
                       AND (? IS NULL OR requested_date <= ?)
                     ORDER BY requested_date DESC, saved_at DESC
                     LIMIT 1
                    """,
                    (sector_type, max_date, max_date),
                ).fetchone()
        if row is None:
            return None
        try:
            report = json.loads(row["report_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(report, dict):
            return None
        report["saved_at"] = str(row["saved_at"])
        return report

    @staticmethod
    def _dataset_sql(
        column: str = "data_source", code_column: str | None = None
    ) -> str:
        legacy_network = (
            f"WHEN {code_column} LIKE 'BK%' AND {column} IN ('market_api', 'cached_market_data') THEN 'akshare'"
            if code_column
            else ""
        )
        return f"""CASE
            WHEN {column} = 'tdx_local' THEN 'tdx_standard'
            WHEN {column} = 'tdx_protocol' THEN 'tdx_online'
            WHEN {column} IN ('tdx', 'tdx_standard', 'tdx_online', 'akshare', 'efinance')
                THEN {column}
            WHEN {column} LIKE 'snapshot%' OR {column} = 'import' THEN 'snapshot'
            {legacy_network}
            ELSE 'legacy'
        END"""

    def _migrate_dataset_schema(self, conn: sqlite3.Connection) -> None:
        """Add dataset identity to primary keys while retaining every old row."""
        daily_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sector_daily)")
        }
        if "dataset_id" not in daily_columns:
            conn.executescript(
                """
                CREATE TABLE sector_daily_v2 (
                    dataset_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    sector_type TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    close REAL, pct_change REAL, amount REAL, volume REAL,
                    rps50 REAL, rank INTEGER, source_rank INTEGER,
                    source_rps50 REAL, data_source TEXT, fetched_at TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, trade_date, sector_type, sector_code)
                );
                """
            )
            conn.execute(
                f"""
                INSERT INTO sector_daily_v2
                SELECT {self._dataset_sql(code_column='sector_code')}, trade_date, sector_type, sector_code,
                       sector_name, close, pct_change, amount, volume, rps50, rank,
                       source_rank, source_rps50, data_source, fetched_at
                  FROM sector_daily
                """
            )
            conn.executescript(
                """
                DROP TABLE sector_daily;
                ALTER TABLE sector_daily_v2 RENAME TO sector_daily;
                CREATE INDEX idx_sector_daily_date
                    ON sector_daily (dataset_id, sector_type, trade_date);
                """
            )

        catalog_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sector_catalog)")
        }
        if "dataset_id" not in catalog_columns:
            conn.executescript(
                """
                CREATE TABLE sector_catalog_v2 (
                    dataset_id TEXT NOT NULL,
                    sector_type TEXT NOT NULL,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    code_system TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, sector_type, sector_code)
                );
                """
            )
            conn.execute(
                f"""
                INSERT INTO sector_catalog_v2
                SELECT {self._dataset_sql()}, sector_type, sector_code, sector_name,
                       code_system, data_source, updated_at
                  FROM sector_catalog
                """
            )
            conn.executescript(
                """
                DROP TABLE sector_catalog;
                ALTER TABLE sector_catalog_v2 RENAME TO sector_catalog;
                CREATE INDEX idx_sector_catalog_name
                    ON sector_catalog (dataset_id, sector_type, sector_name);
                """
            )

    def upsert_sector_daily(self, rows: Iterable[dict]) -> int:
        values = []
        fetched_at = _now()
        for row in rows:
            data_source = str(
                row.get("data_source") or row.get("source") or "market_api"
            )
            source_rank = row.get("source_rank", row.get("raw_rank"))
            source_rps50 = row.get("source_rps50", row.get("raw_rps50"))
            # An explicitly imported snapshot may use the short names rank and
            # rps50 for its original values.  Normal provider rows do not.
            if data_source in {"snapshot", "import", "snapshot_import"} or data_source.startswith("snapshot"):
                source_rank = source_rank if source_rank is not None else row.get("rank")
                source_rps50 = (
                    source_rps50 if source_rps50 is not None else row.get("rps50")
                )
            values.append(
                (
                    str(row.get("dataset_id") or _dataset_id(data_source)),
                    str(row["trade_date"]),
                    str(row["sector_type"]),
                    str(row["sector_code"]),
                    str(row["sector_name"]),
                    _number(row.get("close")),
                    _number(row.get("pct_change")),
                    _number(row.get("amount")),
                    _number(row.get("volume")),
                    _number(row.get("rps50")),
                    _integer(row.get("rank")),
                    _integer(source_rank),
                    _number(source_rps50),
                    str(data_source),
                    fetched_at,
                )
            )
        if not values:
            return 0
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO sector_daily (
                    dataset_id, trade_date, sector_type, sector_code, sector_name,
                    close, pct_change, amount, volume, rps50, rank,
                    source_rank, source_rps50, data_source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id, trade_date, sector_type, sector_code) DO UPDATE SET
                    sector_name=excluded.sector_name,
                    close=excluded.close,
                    pct_change=excluded.pct_change,
                    amount=excluded.amount,
                    volume=excluded.volume,
                    rps50=COALESCE(excluded.rps50, sector_daily.rps50),
                    rank=COALESCE(excluded.rank, sector_daily.rank),
                    source_rank=COALESCE(excluded.source_rank, sector_daily.source_rank),
                    source_rps50=COALESCE(excluded.source_rps50, sector_daily.source_rps50),
                    data_source=CASE
                        WHEN excluded.source_rank IS NOT NULL
                          OR excluded.source_rps50 IS NOT NULL
                            THEN excluded.data_source
                        WHEN sector_daily.source_rank IS NOT NULL
                          OR sector_daily.source_rps50 IS NOT NULL
                            THEN sector_daily.data_source
                        ELSE COALESCE(excluded.data_source, sector_daily.data_source)
                    END,
                    fetched_at=excluded.fetched_at
                """,
                values,
            )
        return len(values)

    def upsert_sector_catalog(self, rows: Iterable[dict]) -> int:
        """Save the provider's board directory without requiring price history."""
        values = []
        updated_at = _now()
        seen = set()
        for row in rows:
            sector_type = str(row.get("sector_type") or "").strip()
            sector_code = str(row.get("sector_code") or "").strip()
            sector_name = str(row.get("sector_name") or "").strip()
            if not sector_type or not sector_code or not sector_name:
                continue
            data_source = str(
                row.get("data_source") or row.get("source") or "provider"
            ).strip()
            dataset_id = str(row.get("dataset_id") or _dataset_id(data_source))
            key = (dataset_id, sector_type, sector_code)
            if key in seen:
                continue
            seen.add(key)
            code_system = str(
                row.get("code_system") or row.get("source") or "provider"
            ).strip()
            values.append(
                (
                    dataset_id,
                    sector_type,
                    sector_code,
                    sector_name,
                    code_system or "provider",
                    data_source or "provider",
                    updated_at,
                )
            )
        if not values:
            return 0
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO sector_catalog (
                    dataset_id, sector_type, sector_code, sector_name,
                    code_system, data_source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id, sector_type, sector_code) DO UPDATE SET
                    sector_name=excluded.sector_name,
                    code_system=excluded.code_system,
                    data_source=excluded.data_source,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def count_sector_catalog(
        self,
        sector_type: str,
        code_systems: str | Iterable[str] | None = None,
        dataset_id: str | None = None,
    ) -> int:
        clauses = ["sector_type = ?"]
        params: list[object] = [sector_type]
        if dataset_id:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        if code_systems:
            if isinstance(code_systems, str):
                values = [code_systems]
            else:
                values = [str(item).strip() for item in code_systems if str(item).strip()]
            if values:
                clauses.append(f"code_system IN ({','.join('?' for _ in values)})")
                params.extend(values)
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM sector_catalog WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row[0])

    def get_sector_catalog(
        self,
        sector_type: str,
        code_system: str | None = None,
        dataset_id: str | None = None,
    ) -> list[dict]:
        clauses = ["sector_type = ?"]
        params: list[object] = [sector_type]
        if dataset_id:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        if code_system:
            clauses.append("code_system = ?")
            params.append(code_system)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT sector_code, sector_name
                  FROM sector_catalog
                 WHERE {' AND '.join(clauses)}
                 ORDER BY sector_code
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def search_sectors(
        self,
        sector_type: str,
        query: str,
        limit: int = 50,
        code_systems: Iterable[str] | None = None,
        dataset_id: str | None = None,
    ) -> list[dict]:
        """Search saved directory entries and local history by code or name."""
        query = query.strip().lower()
        if not query:
            return []
        limit = max(1, min(int(limit), 100))
        pattern = f"%{query}%"
        prefix = f"{query}%"
        catalog_clause = "sector_type = ?"
        catalog_params: list[object] = [sector_type]
        history_clause = "d.sector_type = ?"
        history_params: list[object] = [sector_type]
        latest_clause = "sector_type = ?"
        latest_params: list[object] = [sector_type]
        if dataset_id:
            catalog_clause += " AND dataset_id = ?"
            catalog_params.append(dataset_id)
            history_clause += " AND d.dataset_id = ?"
            history_params.append(dataset_id)
            latest_clause += " AND dataset_id = ?"
            latest_params.append(dataset_id)
        systems = [str(item).strip() for item in (code_systems or []) if str(item).strip()]
        if systems:
            catalog_clause += f" AND code_system IN ({','.join('?' for _ in systems)})"
            catalog_params.extend(systems)
        sql = """
            WITH boards AS (
                SELECT sector_type, sector_code, sector_name,
                       code_system, data_source
                  FROM sector_catalog
                 WHERE {catalog_clause}
                UNION
                SELECT d.sector_type, d.sector_code, d.sector_name,
                       'local_history', COALESCE(d.data_source, 'local_history')
                  FROM sector_daily d
                 WHERE {history_clause}
                   AND NOT EXISTS (
                       SELECT 1
                         FROM sector_catalog c
                        WHERE c.dataset_id = d.dataset_id
                          AND c.sector_type = d.sector_type
                          AND c.sector_code = d.sector_code
                   )
                 GROUP BY d.sector_type, d.sector_code, d.sector_name,
                          d.data_source
            ), latest_dates AS (
                SELECT sector_type, sector_code, MAX(trade_date) AS trade_date
                  FROM sector_daily
                 WHERE {latest_clause}
                 GROUP BY sector_type, sector_code
            ), latest AS (
                SELECT d.sector_type, d.sector_code, d.trade_date,
                       d.close, d.pct_change, d.amount, d.rps50, d.rank
                  FROM sector_daily d
                  JOIN latest_dates x
                    ON x.sector_type = d.sector_type
                   AND x.sector_code = d.sector_code
                   AND x.trade_date = d.trade_date
            )
            SELECT b.sector_type, b.sector_code, b.sector_name,
                   b.code_system, b.data_source,
                   CASE WHEN l.trade_date IS NULL THEN 0 ELSE 1 END AS has_history,
                   l.trade_date AS latest_trade_date,
                   l.close, l.pct_change, l.amount, l.rps50, l.rank
              FROM boards b
              LEFT JOIN latest l
                ON l.sector_type = b.sector_type
               AND l.sector_code = b.sector_code
             WHERE b.sector_type = ?
               AND (lower(b.sector_code) LIKE ? OR lower(b.sector_name) LIKE ?)
             ORDER BY CASE
                        WHEN lower(b.sector_code) = ? THEN 0
                        WHEN lower(b.sector_name) = ? THEN 1
                        WHEN lower(b.sector_code) LIKE ? THEN 2
                        WHEN lower(b.sector_name) LIKE ? THEN 3
                        ELSE 4
                      END,
                      b.sector_name, b.sector_code
             LIMIT ?
        """
        params = tuple(
            catalog_params
            + history_params
            + latest_params
            + [
                sector_type,
                pattern,
                pattern,
                query,
                query,
                prefix,
                prefix,
                limit,
            ]
        )
        sql = sql.format(
            catalog_clause=catalog_clause,
            history_clause=history_clause,
            latest_clause=latest_clause,
        )
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def search_sectors_many(
        self,
        sector_type: str,
        queries: Iterable[str],
        limit: int = 50,
        code_systems: Iterable[str] | None = None,
        dataset_id: str | None = None,
    ) -> tuple[list[dict], list[str]]:
        """Search multiple terms, preserving term order and reporting misses."""
        terms: list[str] = []
        seen_terms: set[str] = set()
        for value in queries:
            term = str(value).strip()
            key = term.lower()
            if term and key not in seen_terms:
                seen_terms.add(key)
                terms.append(term)

        limit = max(1, min(int(limit), 100))
        results: list[dict] = []
        seen_rows: set[tuple[str, str]] = set()
        matched_terms: set[str] = set()
        for term in terms:
            rows = self.search_sectors(
                sector_type,
                term,
                limit,
                code_systems=code_systems,
                dataset_id=dataset_id,
            )
            if rows:
                matched_terms.add(term.lower())
            for row in rows:
                key = (str(row.get("sector_type", sector_type)), str(row.get("sector_code", "")))
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                if len(results) < limit:
                    results.append(row)

        unmatched = [term for term in terms if term.lower() not in matched_terms]
        return results, unmatched

    def update_metrics(self, rows: Iterable[dict]) -> int:
        values = []
        for row in rows:
            values.append(
                (
                    _number(row.get("rps50")),
                    _integer(row.get("rank")),
                    str(row.get("dataset_id") or "legacy"),
                    str(row["trade_date"]),
                    str(row["sector_type"]),
                    str(row["sector_code"]),
                )
            )
        if not values:
            return 0
        with self.connection() as conn:
            conn.executemany(
                """
                UPDATE sector_daily
                   SET rps50 = ?, rank = ?
                 WHERE dataset_id = ? AND trade_date = ?
                   AND sector_type = ? AND sector_code = ?
                """,
                values,
            )
        return len(values)

    def get_sector_rows(
        self,
        sector_type: str,
        start_date: str | None = None,
        end_date: str | None = None,
        dataset_id: str | None = None,
    ) -> list[dict]:
        clauses = ["sector_type = ?"]
        params: list[object] = [sector_type]
        if dataset_id:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        query = f"""
            SELECT dataset_id, trade_date, sector_type, sector_code, sector_name,
                   close, pct_change, amount, volume, rps50, rank,
                   source_rank, source_rps50, data_source, fetched_at
              FROM sector_daily
             WHERE {' AND '.join(clauses)}
             ORDER BY trade_date, sector_code
        """
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_dates(self, sector_type: str, dataset_id: str | None = None) -> list[str]:
        clauses = ["sector_type = ?"]
        params: list[object] = [sector_type]
        if dataset_id:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT trade_date
                  FROM sector_daily
                 WHERE {' AND '.join(clauses)}
                 ORDER BY trade_date DESC
                """,
                params,
            ).fetchall()
        return [str(row[0]) for row in rows]

    def save_sync_run(
        self,
        sector_type: str,
        provider: str,
        requested_sectors: int,
        succeeded_sectors: int,
        saved_rows: int,
        errors: list[str],
        started_at: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_runs (
                    started_at, finished_at, sector_type, provider,
                    requested_sectors, succeeded_sectors, saved_rows, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    _now(),
                    sector_type,
                    provider,
                    requested_sectors,
                    succeeded_sectors,
                    saved_rows,
                    json.dumps(errors[:50], ensure_ascii=False),
                ),
            )

    def ensure_formula(
        self,
        name: str,
        formula: str,
        *,
        is_builtin: bool = False,
    ) -> dict:
        """Create a formula once and return the stored definition."""
        name = str(name or "").strip()
        formula = str(formula or "").strip()
        if not name or not formula:
            raise ValueError("公式名称和内容不能为空")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM formula_definitions WHERE name = ?",
                (name,),
            ).fetchone()
            if row is not None:
                return dict(row)
            now = _now()
            cursor = conn.execute(
                """
                INSERT INTO formula_definitions
                    (name, formula, is_builtin, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, formula, 1 if is_builtin else 0, now, now),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM formula_definitions WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            )

    def save_formula(
        self,
        name: str,
        formula: str,
        formula_id: int | None = None,
    ) -> dict:
        name = str(name or "").strip()
        formula = str(formula or "").strip()
        if not name:
            raise ValueError("公式名称不能为空")
        if not formula:
            raise ValueError("公式内容不能为空")
        with self.connection() as conn:
            now = _now()
            if formula_id is None:
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO formula_definitions
                            (name, formula, is_builtin, created_at, updated_at)
                        VALUES (?, ?, 0, ?, ?)
                        """,
                        (name, formula, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"已经存在同名公式：{name}") from exc
                formula_id = int(cursor.lastrowid)
            else:
                row = conn.execute(
                    "SELECT is_builtin FROM formula_definitions WHERE id = ?",
                    (int(formula_id),),
                ).fetchone()
                if row is None:
                    raise ValueError("公式不存在，可能已被删除")
                if int(row[0]) == 1:
                    raise ValueError("内置公式不能覆盖；请另存为一个新公式")
                try:
                    conn.execute(
                        """
                        UPDATE formula_definitions
                           SET name = ?, formula = ?, updated_at = ?
                         WHERE id = ?
                        """,
                        (name, formula, now, int(formula_id)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"已经存在同名公式：{name}") from exc
            stored = conn.execute(
                "SELECT * FROM formula_definitions WHERE id = ?",
                (int(formula_id),),
            ).fetchone()
        return dict(stored)

    def list_formulas(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM formula_definitions ORDER BY is_builtin DESC, name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_formula(self, formula_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM formula_definitions WHERE id = ?",
                (int(formula_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_formula(self, formula_id: int) -> None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT is_builtin FROM formula_definitions WHERE id = ?",
                (int(formula_id),),
            ).fetchone()
            if row is None:
                raise ValueError("公式不存在，可能已被删除")
            if int(row[0]) == 1:
                raise ValueError("内置公式不能删除")
            conn.execute("DELETE FROM formula_definitions WHERE id = ?", (int(formula_id),))

    def save_formula_run(self, run: dict) -> int:
        result = dict(run.get("result") or {})
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO formula_runs (
                    formula_id, formula_name, formula, timeframe, provider,
                    data_source, scanned_count, technical_candidates,
                    fundamental_verified, warnings_json, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.get("formula_id"),
                    str(run.get("formula_name") or "未命名公式"),
                    str(run.get("formula") or ""),
                    str(run.get("timeframe") or ""),
                    str(run.get("provider") or "tdx_standard"),
                    str(result.get("data_source") or ""),
                    int(result.get("scanned_count") or 0),
                    int(result.get("technical_candidates") or 0),
                    int(result.get("fundamental_verified") or 0),
                    json.dumps(warnings, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    _now(),
                ),
            )
        return int(cursor.lastrowid)

    def list_formula_runs(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, formula_id, formula_name, timeframe, provider,
                       data_source, scanned_count, technical_candidates,
                       fundamental_verified, warnings_json, created_at
                  FROM formula_runs
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
            except (TypeError, ValueError):
                item["warnings"] = []
            result.append(item)
        return result

    def get_formula_run(self, run_id: int) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM formula_runs WHERE id = ?",
                (int(run_id),),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        except (TypeError, ValueError):
            item["warnings"] = []
        try:
            item["result"] = json.loads(item.pop("result_json") or "{}")
        except (TypeError, ValueError):
            item["result"] = {}
        return item

    def delete_formula_run(self, run_id: int) -> bool:
        """Delete one saved screening run without touching formulas or market data."""
        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM formula_runs WHERE id = ?",
                (int(run_id),),
            )
        return cursor.rowcount > 0

    def clear_capital_flow_data(self, *, cache_only: bool = False) -> dict[str, Any]:
        """Back up and remove only flow caches, never market or screening data."""
        tables = ("sector_capital_flow_daily", "sector_capital_flow_reports",
                  "sector_capital_flow_catalog", "sector_capital_flow_current", "imported_capital_flow_daily", "capital_flow_jobs", "capital_flow_revisions", "capital_flow_collection")
        if cache_only:
            tables = ("sector_capital_flow_reports", "sector_capital_flow_current")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._guard_flow_jobs(conn)
            snapshot = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                        for table in tables}
            count = sum(len(rows) for rows in snapshot.values())
            backup_path = None
            if count:
                folder = self.path.parent / "capital-flow-backups"
                folder.mkdir(parents=True, exist_ok=True)
                backup_path = folder / f"capital-flow-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
                # Backup failure aborts the transaction before any deletion.
                with backup_path.open("x", encoding="utf-8") as stream:
                    json.dump(snapshot, stream, ensure_ascii=False)
            for table in tables:
                conn.execute(f"DELETE FROM {table}")
        return {"deleted_rows": count, "backup_path": str(backup_path) if backup_path else None}

    def clear_sector_data(self, sector_type: str | None = None) -> int:
        """Delete local market data and sync records, returning deleted rows."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._guard_flow_jobs(conn)
            if sector_type is None or sector_type == "all":
                count = conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0]
                count += conn.execute("SELECT COUNT(*) FROM sector_capital_flow_daily").fetchone()[0]
                count += conn.execute("SELECT COUNT(*) FROM sector_capital_flow_reports").fetchone()[0]
                count += conn.execute("SELECT COUNT(*) FROM sector_capital_flow_catalog").fetchone()[0]
                conn.execute("DELETE FROM sector_daily")
                conn.execute("DELETE FROM sync_runs")
                conn.execute("DELETE FROM sector_capital_flow_daily")
                conn.execute("DELETE FROM sector_capital_flow_reports")
                conn.execute("DELETE FROM sector_capital_flow_catalog")
                conn.execute("DELETE FROM sector_capital_flow_current")
                conn.execute("DELETE FROM capital_flow_jobs")
                return int(count)

            count = conn.execute(
                "SELECT COUNT(*) FROM sector_daily WHERE sector_type = ?",
                (sector_type,),
            ).fetchone()[0]
            count += conn.execute(
                "SELECT COUNT(*) FROM sector_capital_flow_daily WHERE sector_type = ?",
                (sector_type,),
            ).fetchone()[0]
            count += conn.execute(
                "SELECT COUNT(*) FROM sector_capital_flow_reports WHERE sector_type = ?",
                (sector_type,),
            ).fetchone()[0]
            count += conn.execute(
                "SELECT COUNT(*) FROM sector_capital_flow_catalog WHERE sector_type = ?",
                (sector_type,),
            ).fetchone()[0]
            conn.execute("DELETE FROM sector_daily WHERE sector_type = ?", (sector_type,))
            conn.execute("DELETE FROM sync_runs WHERE sector_type = ?", (sector_type,))
            conn.execute("DELETE FROM sector_capital_flow_daily WHERE sector_type = ?", (sector_type,))
            conn.execute("DELETE FROM sector_capital_flow_reports WHERE sector_type = ?", (sector_type,))
            conn.execute("DELETE FROM sector_capital_flow_catalog WHERE sector_type = ?", (sector_type,))
            conn.execute("DELETE FROM sector_capital_flow_current WHERE sector_type = ?", (sector_type,))
            conn.execute("DELETE FROM capital_flow_jobs WHERE sector_type = ?", (sector_type,))
            return int(count)


def _number(value):
    if value is None:
        return None
    try:
        number = float(value)
        if number != number:
            return None
        return number
    except (TypeError, ValueError):
        return None


def _integer(value):
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
