"""Daily snapshots are the primary history; remote history is optional backfill.

No public central feed is enabled until the publisher has redistribution rights.
All mirror payloads are dated, source-scoped, bounded and hash-checked before SQL.
"""
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
import math
import threading
import time
from urllib.parse import urlparse
import uuid

from app.capital_flow import fetch_sector_capital_flow, share_current_flow_with_history, fetch_sector_capital_flow_report
from app.flow_calendar import confirmed_close_date, latest_closed_trading_day, shanghai_now, trading_window, is_trading_day
from app.providers.quote_fallback import _request_get, close_reused_sessions


def _https(url):
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError("历史库地址必须是无凭据的公网HTTPS地址")
    if parsed.hostname == "localhost" or parsed.hostname.endswith((".local", ".localhost")):
        raise ValueError("历史库地址不允许本机地址")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global: raise ValueError("历史库地址不允许内网地址")
    return str(url)


def _read(url, deadline, max_bytes):
    response = _request_get(_https(url), _deadline=deadline, timeout=(3,5), stream=True,
                            headers={"Accept":"application/json"}, allow_redirects=False)
    try:
        response.raise_for_status()
        if response.status_code != 200: raise ValueError("历史库只接受直接200响应")
        data = bytearray()
        for chunk in response.iter_content(65536):
            if time.monotonic() >= deadline: raise TimeoutError("历史库同步达到等待上限")
            data.extend(chunk)
            if len(data) > max_bytes: raise ValueError("历史库文件超出大小限制")
        return bytes(data)
    finally:
        response.close()


def validate_daily(payload, sector_type, day):
    if not isinstance(payload,dict) or any(payload.get(key)!=value for key,value in {
        "schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY",
        "sector_type":sector_type,"trade_date":day,"closed":True}.items()):
        raise ValueError("历史库来源、口径或日期不匹配")
    if is_trading_day(date.fromisoformat(day)) is not True: raise ValueError("历史库交易日未确认")
    rows = payload.get("rows")
    if not isinstance(rows,list) or not 1 <= len(rows) <= 5000 or payload.get("total") != len(rows):
        raise ValueError("历史库板块数量不完整")
    codes, clean = set(), []
    fields = ("latest_price","pct_change","main_net_inflow","main_net_ratio","super_large_net_inflow",
              "super_large_ratio","large_net_inflow","large_ratio","medium_net_inflow","medium_ratio","small_net_inflow","small_ratio")
    for row in rows:
        if not isinstance(row,dict): raise ValueError("历史库记录格式错误")
        code, name = row.get("sector_code"), row.get("sector_name")
        if not isinstance(code,str) or not code.startswith("BK") or not code[2:].isdigit() or code in codes or not isinstance(name,str) or not name.strip() or len(name)>200:
            raise ValueError("历史库板块标识重复或无效")
        if confirmed_close_date(row.get("source_time"), captured_at=payload.get("captured_at")) != day:
            raise ValueError("历史库记录不是已确认的收盘数据")
        item = {"sector_type":sector_type,"trade_date":day,"sector_code":code,"sector_name":name}
        for field in fields:
            value = row.get(field)
            if value is not None and (isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value)):
                raise ValueError("历史库金额或比例不是有限数值")
            item[field] = value
        if item["main_net_inflow"] is None: raise ValueError("历史库缺少主力净流入")
        clean.append(item); codes.add(code)
    return clean


def sync_feed(database, sector_type, cutoff, urls, *, cancel=None):
    result = {"configured":bool(urls),"saved_days":[],"errors":[]}
    if not urls: return result
    deadline = time.monotonic()+45
    index = None
    for url in urls[:3]:
        try:
            candidate = json.loads(_read(url,deadline,256*1024))
            if any(candidate.get(k)!=v for k,v in {"schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY","redistribution_authorized":True}.items()):
                raise ValueError("历史库未声明分发授权或口径不符")
            if not isinstance(candidate.get("entries"),list) or len(candidate["entries"])>10000: raise ValueError("历史库索引过大或无效")
            index = candidate; break
        except Exception as exc: result["errors"].append(str(exc))
    if index is None: return result
    wanted = set(trading_window(date.fromisoformat(cutoff)) or [cutoff])
    used = set()
    for entry in index["entries"]:
        if time.monotonic() >= deadline or (cancel and cancel.is_set()): break
        if not isinstance(entry,dict) or entry.get("sector_type") != sector_type or entry.get("trade_date") not in wanted: continue
        day = entry["trade_date"]
        if day in used: continue
        used.add(day)
        sha = entry.get("sha256")
        mirrors = entry.get("urls")
        if not isinstance(sha,str) or len(sha)!=64 or any(c not in "0123456789abcdef" for c in sha) or not isinstance(mirrors,list):
            result["errors"].append(f"{day}索引校验值或下载地址无效"); continue
        # Compare payload hash, not only row count, so upstream revisions sync.
        with database.connection() as conn:
            stored = conn.execute("SELECT state_json FROM capital_flow_collection WHERE sector_type=? AND trade_date=?",(sector_type,day)).fetchone()
        if stored and json.loads(stored[0]).get("feed_sha256")==sha: continue
        for url in mirrors[:3]:
            try:
                if cancel and cancel.is_set(): break
                data = _read(url,deadline,2*1024*1024)
                if hashlib.sha256(data).hexdigest() != sha: raise ValueError("历史库校验值不匹配，未保存")
                rows = validate_daily(json.loads(data),sector_type,day)
                database.upsert_sector_capital_flow_daily(rows)
                database.save_sector_capital_flow_catalog(sector_type,rows)
                with database.connection() as conn:
                    # Do not revoke a running local collector's lease.
                    old = conn.execute("SELECT state_json FROM capital_flow_collection WHERE sector_type=? AND trade_date=?",(sector_type,day)).fetchone()
                    state = json.loads(old[0]) if old else {}
                    state.update(feed_sha256=sha, feed_rows=len(rows))
                    conn.execute("INSERT INTO capital_flow_collection VALUES(?,?,?,'',0,0) ON CONFLICT(sector_type,trade_date) DO UPDATE SET state_json=excluded.state_json",(sector_type,day,json.dumps(state)))
                result["saved_days"].append(day); break
            except Exception as exc: result["errors"].append(f"{day}: {exc}")
    return result


def update_daily(database, sector_type, selected_date, *, feed_urls=(), cancel=None, progress=None):
    """One click does bounded collection+sync, then computes purely from storage."""
    closed, _ = latest_closed_trading_day(date.fromisoformat(selected_date))
    notes = []
    current = None
    if not cancel or not cancel.is_set():
        try:
            current = collect_current(database,sector_type)
            if progress: progress({"current_updated":True,"current_total":current.get("total",0),"stage":"sync"})
        except Exception as exc: notes.append(f"当天榜联网失败，原数据保留：{exc}")
    feed = sync_feed(database,sector_type,closed.isoformat(),list(feed_urls),cancel=cancel)
    if feed["errors"]: notes.append("集中历史库部分下载失败，校验失败的数据未写入，继续使用本地记录。")
    try:
        report = fetch_sector_capital_flow_report(sector_type,1000,selected_date,database,local_only=True)
    except RuntimeError:
        report = {"ok":True,"sector_type":sector_type,"requested_date":selected_date,
                  "resolved_date":closed.isoformat(),"report_date":None,"rows":[],"total":0,
                  "window_dates":[],"inflow_days_rank":[],"window_complete":False,"date_confirmed":False,
                  "daily_complete":False,"catalog_complete":False,"partial":True,"source":"东方财富本地逐日档案",
                  "history_coverage":{"requested_boards":(current or {}).get("total",0),"complete_boards":0,"window_days":0},
                  "warnings":["尚无已确认的收盘档案；当天盘中榜可正常查看。需要收盘后采集，缺失日期不会被伪造。"]}
    report["warnings"] = list(report.get("warnings") or []) + notes
    if not feed_urls and not report.get("window_complete"):
        report["warnings"].append("集中历史库尚未启用：每天收盘后自动积累本机档案。未开机日期需从可用历史源补齐，不能保证过去日期可取得。")
    report["feed_sync"] = feed
    report["current_updated"] = current is not None
    report["refresh_error"] = notes[0] if notes else ""
    return report


def collect_current(database, sector_type):
    current = fetch_sector_capital_flow(sector_type,1000)
    current = share_current_flow_with_history(current,database)
    database.save_current_capital_flow({**current,"kind":"current"})
    database.save_sector_capital_flow_catalog(sector_type,current["rows"])
    return current


class DailyCollector:
    """App-open post-close timer. Durable leases deduplicate multiple windows."""
    def __init__(self,database, run_guard=lambda fn:fn):
        self.database, self.run_guard = database, run_guard
        self.stop = threading.Event(); self.thread = None; self.owner = uuid.uuid4().hex

    def tick(self, now=None):
        now = now or shanghai_now()
        if is_trading_day(now.date()) is not True or (now.hour,now.minute)<(15,10): return
        for sector_type in ("concept","industry"):
            if self.stop.is_set(): break
            day, stamp = now.date().isoformat(), time.time()
            with self.database.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT * FROM capital_flow_collection WHERE sector_type=? AND trade_date=?",(sector_type,day)).fetchone()
                state = json.loads(old["state_json"]) if old else {}
                if old and (old["lease_until"]>stamp or old["next_attempt"]>stamp): continue
                if state.get("attempts",0)>=6 or state.get("successful_passes",0)>=2: continue
                state["attempts"] = state.get("attempts",0)+1
                conn.execute("INSERT INTO capital_flow_collection VALUES(?,?,?,?,?,?) ON CONFLICT(sector_type,trade_date) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until,state_json=excluded.state_json",(sector_type,day,json.dumps(state),self.owner,stamp+120,stamp+600))
            try:
                current = self.run_guard(collect_current)(self.database,sector_type)
                if current.get("archive_complete") and current.get("confirmed_trade_date")==day:
                    state["successful_passes"] = state.get("successful_passes",0)+1
                    state["rows"] = current.get("closed_rows_saved",0)
                    state["last_error"] = ""
                else: state["last_error"] = "来源尚未提供当天完整收盘数据，稍后复核"
            except Exception as exc: state["last_error"] = str(exc)
            finally:
                state["checked_at"] = datetime.now(timezone.utc).isoformat()
                with self.database.connection() as conn:
                    conn.execute("UPDATE capital_flow_collection SET state_json=?,lease_until=0,next_attempt=? WHERE sector_type=? AND trade_date=? AND owner=?",(json.dumps(state),time.time()+600,sector_type,day,self.owner))
                close_reused_sessions()

    def start(self):
        if self.thread and self.thread.is_alive(): return
        def run():
            while not self.stop.is_set():
                try: self.tick()
                except Exception: pass  # One DB/transport failure must not kill tomorrow's timer.
                self.stop.wait(60)
        self.thread = threading.Thread(target=run,daemon=True,name="flow-daily-archive")
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=2)
