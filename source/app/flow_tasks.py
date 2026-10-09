"""Bounded, persistent updates with cancellation and cross-process leases."""
from datetime import date
from threading import Event, Lock, Thread
import time
import uuid

from app.capital_flow import (fetch_sector_capital_flow_report, fetch_sector_capital_flow,
                              share_current_flow_with_history, validate_flow_rule)
from app.flow_calendar import shanghai_now
from app.flow_archive import update_daily
from app.flow_transport import HISTORY_POOL
from app.providers.quote_fallback import close_reused_sessions

MAX_ROUNDS = 3
JOB_SECONDS = 1800
ROUND_SECONDS = 600


class _CancelToken:
    def __init__(self, event, stop, database, job_id):
        self.event, self.stop, self.database, self.job_id = event, stop, database, job_id
        self.checked_at = 0

    def is_set(self):
        if self.event.is_set() or self.stop.is_set():
            return True
        if time.monotonic()-self.checked_at >= .5:
            self.checked_at = time.monotonic()
            saved = self.database.get_flow_job(self.job_id)
            if saved is None or saved.get("cancel_requested"):
                self.event.set()
        return self.event.is_set()


class FlowHistoryJobs:
    def __init__(self):
        self.lock, self.jobs = Lock(), {}
        self.owner = uuid.uuid4().hex
        self.stop = Event()
        self.recovery_thread = None
        self.workers = {}

    def _snapshot(self, job_id):
        return {k:v for k,v in self.jobs[job_id].items() if k not in {"cancel","database"}}

    def snapshot(self, job_id, database=None):
        with self.lock:
            if job_id in self.jobs:
                return self._snapshot(job_id)
        saved = database.get_flow_job(job_id) if database else None
        if saved is None:
            raise KeyError(job_id)
        try:
            saved["result"] = fetch_sector_capital_flow_report(saved["sector_type"],1000,saved["date"],database,local_only=True,
                window_days=saved.get("window_days",10),min_inflow_days=saved.get("min_inflow_days",6))
        except RuntimeError:
            pass
        return saved

    def cancel(self, job_id, database=None):
        with self.lock:
            if job_id in self.jobs:
                job = self.jobs[job_id]
                job["database"].cancel_flow_job(job_id)
                job["cancel"].set()
                return self._snapshot(job_id)
        if database is None or not database.cancel_flow_job(job_id):
            raise KeyError(job_id)
        return self.snapshot(job_id,database)

    def is_running(self):
        with self.lock:
            return any(job["status"] == "running" for job in self.jobs.values())

    def start(self, database, sector_type, selected_date, run_guard, *, recovering=False, mode="history", feed_urls=(), window_days=10, min_inflow_days=6):
        validate_flow_rule(window_days,min_inflow_days)
        day = date.fromisoformat(selected_date)
        if sector_type not in {"concept","industry"}:
            raise ValueError("资金流向只支持概念或行业板块")
        if day > shanghai_now().date():
            raise ValueError("查询日期不能晚于今天")
        if self.stop.is_set():
            raise ValueError("软件正在关闭，请重新打开后更新")
        with self.lock:
            for job_id,job in self.jobs.items():
                if job["status"] == "running" and (job["sector_type"],job["date"]) == (sector_type,selected_date):
                    if (job.get("window_days",10),job.get("min_inflow_days",6)) != (window_days,min_inflow_days):
                        raise ValueError("该日期正在按另一组筛选条件更新，请完成或取消后调整条件")
                    return self._snapshot(job_id)
            if sum(j["status"] == "running" for j in self.jobs.values()) >= 2:
                raise ValueError("已有两个资金更新任务，请完成或取消后再启动")
            initial = {"id":uuid.uuid4().hex,"status":"running","sector_type":sector_type,"date":selected_date,
                       "mode":mode,"window_days":window_days,"min_inflow_days":min_inflow_days,
                       "attempted":0,"downloaded":0,"failed":0,"total":0,"round":0,"max_rounds":MAX_ROUNDS,
                       "created_at":time.time(),"expires_at":time.time()+JOB_SECONDS,"next_retry_at":0}
            state,claimed = database.claim_flow_job(initial,self.owner,restart_expired=not recovering)
            # A second process can own this date; never silently show its rule.
            if (state.get("window_days",10),state.get("min_inflow_days",6)) != (window_days,min_inflow_days):
                raise ValueError("该日期正在按另一组筛选条件更新，请完成或取消后调整条件")
            if not claimed:
                return state
            job_id,cancel = state["id"],Event()
            if state.get("cancel_requested"):
                cancel.set()
            state["resumed"] = job_id != initial["id"]
            self.jobs[job_id] = {**state,"cancel":cancel,"database":database}
            for old_id in list(self.jobs):
                if len(self.jobs) <= 20:
                    break
                if self.jobs[old_id]["status"] != "running":
                    del self.jobs[old_id]
                    self.workers.pop(old_id,None)

        token = _CancelToken(cancel,self.stop,database,job_id)

        def update(values):
            with self.lock:
                self.jobs[job_id].update(values)
                snapshot = self._snapshot(job_id)
                # Do not expose "complete" before its checkpoint has committed.
                if not database.save_flow_job(snapshot,self.owner):
                    cancel.set()

        def wait_until(stamp):
            while time.time() < stamp and not token.is_set():
                self.stop.wait(min(.5,max(0,stamp-time.time())))
            return not token.is_set()

        def complete(report):
            cov = report["history_coverage"]
            return report["date_confirmed"] and report["catalog_complete"] and cov["complete_boards"] == cov["requested_boards"]

        @run_guard
        def work():
            try:
                if token.is_set() or time.time() >= state["expires_at"] or state["round"] >= MAX_ROUNDS:
                    update({"status":"paused" if self.stop.is_set() else "cancelled" if cancel.is_set() else "partial",
                            "message":"任务未继续联网，已保存的数据仍保留；如需重新查询请点击一键更新"})
                    return
                update({"stage":"current","message":"正在联网更新当前资金数据"})
                if state.get("mode") == "daily":
                    report = update_daily(database,sector_type,selected_date,feed_urls=feed_urls,cancel=token,progress=update,
                                          window_days=window_days,min_inflow_days=min_inflow_days)
                    status = "paused" if self.stop.is_set() else "cancelled" if token.is_set() else "complete" if (report.get("current_updated") or report.get("rows") or report.get("feed_sync",{}).get("saved_days")) else "failed"
                    update({"result":report,"status":status,"next_retry_at":0,"message":f"当天数据已保存；{window_days}日榜只使用完整逐日档案" if status=="complete" else "未取得新数据，已保存记录保留"})
                    return
                try:
                    current = fetch_sector_capital_flow(sector_type,1000)
                    current = share_current_flow_with_history(current,database)
                    database.save_current_capital_flow({**current,"kind":"current"})
                    database.save_sector_capital_flow_catalog(sector_type,current["rows"])
                    update({"current_updated":True,"current_total":len(current["rows"])})
                except Exception as exc:
                    update({"current_updated":False,"current_error":str(exc)})
                try:
                    initial_report = fetch_sector_capital_flow_report(sector_type,1000,selected_date,database,local_only=True,
                        window_days=window_days,min_inflow_days=min_inflow_days)
                    update({"result":initial_report})
                except RuntimeError:
                    pass
                report = None
                last_error = ""
                for round_number in range(state["round"]+1,MAX_ROUNDS+1):
                    if token.is_set() or time.time() >= state["expires_at"]:
                        break
                    retry_at = max(state.get("next_retry_at",0),time.time()+HISTORY_POOL.snapshot()["retry_after_seconds"])
                    if retry_at > time.time():
                        update({"stage":"cooldown","next_retry_at":retry_at,"message":"网络通道冷却中，稍后自动续补"})
                        if retry_at >= state["expires_at"] or not wait_until(retry_at):
                            break
                    update({"stage":"history","round":round_number,"next_retry_at":0})
                    def progress(values):
                        update({**values,"round":round_number,"history_transports":HISTORY_POOL.snapshot()})
                    retry = False
                    retry_after = 0
                    try:
                        report = fetch_sector_capital_flow_report(sector_type,1000,selected_date,database,
                            retry_missing=True,query_seconds=min(ROUND_SECONDS,max(1,state["expires_at"]-time.time())),progress=progress,cancel=token,
                            window_days=window_days,min_inflow_days=min_inflow_days)
                        update({"result":report,"history_transports":report.get("history_transports",{})})
                        if complete(report):
                            update({"status":"complete","message":"下载完成","next_retry_at":0})
                            return
                        retry = report.get("retry_recommended",False)
                        retry_after = report.get("retry_after_seconds",0)
                        last_error = report.get("stopped_reason") or "仍有历史数据缺失"
                    except Exception as exc:
                        last_error = str(exc)
                        retry = getattr(exc,"retryable",False)
                        retry_after = getattr(exc,"retry_after",0)
                    if not retry or token.is_set() or round_number == MAX_ROUNDS:
                        break
                    next_retry = time.time()+max(30 if round_number == 1 else 90,retry_after)
                    state["next_retry_at"] = next_retry
                    update({"stage":"cooldown","next_retry_at":next_retry,"message":f"本轮连接未完成，等待后自动重试（最多{MAX_ROUNDS}轮）"})
                status = "paused" if self.stop.is_set() else "cancelled" if cancel.is_set() else "partial" if report else "failed"
                retry_checkpoint = max(self.jobs[job_id].get("next_retry_at",0),time.time()+HISTORY_POOL.snapshot()["retry_after_seconds"]) if status == "paused" else 0
                update({"status":status,"next_retry_at":retry_checkpoint,"message":
                    "软件关闭，已保存进度；重开后在任务有效期内自动续补" if status == "paused" else
                    "已取消更新，成功取得的数据已保存" if status == "cancelled" else
                    f"自动更新结束，未完整下载；已保存取得的数据。{last_error}"})
            except Exception as exc:
                update({"status":"paused" if self.stop.is_set() else "cancelled" if cancel.is_set() else "failed","message":str(exc)})
            finally:
                close_reused_sessions()

        worker = Thread(target=work,daemon=True,name=f"flow-history-{job_id[:8]}")
        with self.lock:
            self.workers[job_id] = worker
        worker.start()
        return self.snapshot(job_id)

    def resume_pending(self,database,run_guard):
        resumed = []
        for state in database.pending_flow_jobs():
            try:
                from app.config import settings
                resumed.append(self.start(database,state["sector_type"],state["date"],run_guard,recovering=True,mode=state.get("mode","history"),feed_urls=settings.flow_feed_urls,
                    window_days=state.get("window_days",10),min_inflow_days=state.get("min_inflow_days",6)))
            except ValueError:
                break
        return resumed

    def start_recovery(self,database,run_guard):
        if self.recovery_thread and self.recovery_thread.is_alive():
            return
        self.stop.clear()
        def recover():
            while not self.stop.is_set():
                self.resume_pending(database,run_guard)
                with self.lock:
                    running = [(j["database"],self._snapshot(key)) for key,j in self.jobs.items() if j["status"]=="running"]
                for db,state in running:
                    db.save_flow_job(state,self.owner)
                self.stop.wait(30)
        self.recovery_thread = Thread(target=recover,daemon=True,name="flow-update-recovery")
        self.recovery_thread.start()

    def shutdown(self):
        self.stop.set()
        with self.lock:
            workers = list(self.workers.values())
        deadline = time.monotonic()+2
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=max(0,deadline-time.monotonic()))
        if self.recovery_thread and self.recovery_thread.is_alive():
            self.recovery_thread.join(timeout=max(0,deadline-time.monotonic()))
