"""Deterministic, dated watchlist. AI may explain, never select or trade."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import re
import time
import uuid
from threading import Lock, Thread

from . import formula_screen as fs
from .flow_calendar import is_trading_day, latest_closed_trading_day, shanghai_now
from .flow_summary import attach_three_day_flow
from .formula_engine import uses_finance
from .sector_leaders import _fetch_constituents, _local_tdx_block_paths

RULE_VERSION = "watchlist-v1"
PROMPT_VERSION = "candidate-explanation-v1"
DISCLAIMER = "仅为规则候选观察池，得分不是上涨概率，不构成买卖指令；未验证财务、公告及下一交易日可成交性。历史表现不保证未来收益。"


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _key(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def select_boards(database, report, limit, require_flow):
    pool = {}
    for row in report.get("latest_top100", []):
        pool[str(row["sector_code"])] = dict(row)
    for name in ("daily_up", "five_day_up", "five_day_top100_up", "ten_day_six_up"):
        for row in report.get("modules", {}).get(name, {}).get("rows", []):
            pool.setdefault(str(row["sector_code"]), {}).update(row)
    tiers = {}
    for tier, rows in report.get("t_groups", {}).items():
        for row in rows:
            code = str(row["sector_code"])
            tiers.setdefault(code, tier)
            pool.setdefault(code, {}).update(row)
    module = {"rows": list(pool.values())}
    attach_three_day_flow(database, report["sector_type"], report["report_date"], module)
    boards = []
    for row in module["rows"]:
        rps = _number(row.get("rps50"))
        rank = _number(row.get("rank"))
        improvement = max((_number(row.get(k)) or 0) for k in ("rank_change_1d", "rank_change_5d", "rank_change_10d"))
        if rps is None or rps < 60 or rank is None or (rank > 100 and improvement < 15):
            continue
        flow = row.get("three_day_main_net_inflow") if row.get("three_day_flow_status") == "complete" else None
        if require_flow and (flow is None or flow <= 0):
            continue
        tier = tiers.get(str(row["sector_code"]), "未分组")
        # Correlated rank-improvement windows use their maximum, not a sum.
        score = {"T0": 15, "T1": 10, "T2": 5}.get(tier, 0)
        score += 10 if rps >= 85 else 5
        score += 15 if improvement >= 15 else 5 if improvement > 0 else 0
        score += 15 if flow is not None and flow > 0 else 0
        boards.append({**row, "tier": tier, "board_score": score, "flow_reference": row.get("three_day_flow_match") == "unique_name"})
    boards.sort(key=lambda r: (-r["board_score"], r["rank"], str(r["sector_code"])))
    return boards[:limit]


def strict_members(board, sector_type, provider):
    """No fuzzy name mapping: exact local group or verified Eastmoney code."""
    if provider in {"tdx", "tdx_standard", "tdx_online"}:
        from pytdx.reader.block_reader import BlockReader, BlockReader_TYPE_GROUP
        for path in _local_tdx_block_paths(sector_type):
            if not path.is_file():
                continue
            try:
                groups = BlockReader().get_data(str(path), BlockReader_TYPE_GROUP)
            except Exception:
                continue
            matches = [g for g in groups if _key(g.get("blockname")) == _key(board["sector_name"])]
            if len(matches) == 1:
                return {"source": "通达信本地同名板块成员（当前成员，非历史成分）", "rows": [{"code": c.strip(), "name": c.strip()} for c in str(matches[0].get("code_list", "")).split(",") if re.fullmatch(r"\d{6}", c.strip())]}
            if len(matches) > 1:
                raise ValueError("本地同名板块成员存在歧义，未用于候选筛选")
    code = board.get("three_day_flow_code")
    if not code and re.fullmatch(r"BK\d{4}", str(board.get("sector_code", ""))):
        code = board["sector_code"]
    if not code:
        raise ValueError("无法唯一匹配板块成分，未用于候选筛选")
    return {"source": "东方财富当前板块成员（非历史成分）", "rows": _fetch_constituents(code)}


def stock_metrics(bars, report_date, min_amount):
    dated = [b for b in bars if str(b["date"])[:10] <= report_date]
    if len(dated) < 61:
        return None, "历史不足"
    if str(dated[-1]["date"])[:10] != report_date:
        return None, "个股日期与报告不一致"
    latest = dated[-1]
    values = [_number(b.get("close")) for b in dated[-61:]]
    volume = _number(latest.get("volume"))
    amount = _number(latest.get("amount"))
    if any(v is None or v <= 0 for v in values) or volume is None or volume <= 0 or amount is None or amount < min_amount:
        return None, "行情或流动性条件不通过"
    close = values[-1]
    ma20, ma60 = sum(values[-20:])/20, sum(values[-60:])/60
    ret20, ret5 = (close/values[-21]-1)*100, (close/values[-6]-1)*100
    if not all(math.isfinite(v) for v in (ma20, ma60, ret20, ret5)):
        return None, "行情或流动性条件不通过"
    if close <= ma20 or ret20 <= 0 or ret5 > 30:
        return None, "技术条件不通过或短期涨幅过大"
    volumes = [_number(b.get("volume")) for b in dated[-21:-1]]
    ratio = volume/(sum(volumes)/20) if all(v is not None and v > 0 for v in volumes) else None
    if ratio is not None and not math.isfinite(ratio): ratio = None
    score = 20 + (15 if ma20 > ma60 else 0) + (5 if ret5 > 0 else 0) + (5 if ratio is not None and ratio >= 1.2 else 0)
    return {"close": close, "ma20": ma20, "ma60": ma60, "return_20d": ret20, "return_5d": ret5, "volume_ratio": ratio, "amount": amount, "stock_score": score, "bars": dated}, None


def generate_candidates(database, report, options, fetch_members=strict_members, progress=lambda _: None, cancelled=lambda: False, name_fetcher=fs._enrich_names):
    now = shanghai_now()
    closed, _ = latest_closed_trading_day(now.date(), now)
    if report["report_date"] != closed.isoformat() or is_trading_day(date.fromisoformat(report["report_date"])) is not True:
        raise ValueError("候选观察池仅使用最近已收盘交易日，请更新数据并读取最新报告；当前成分不能用于历史回测")
    files = fs._discover_day_files()
    if not files:
        raise ValueError("没有通达信个股日线。请先配置TDX_ROOT并在通达信下载个股日线；不使用板块涨幅冒充个股历史")
    file_map = {code: path for code, path in files}
    boards = select_boards(database, report, options["board_limit"], options["require_flow"])
    warnings = list(report.get("data_status", {}).get("warnings", []))
    warnings.append("成员关系及证券名称为当前资料，不支持历史回测；个股技术数据来自通达信本地日线，资金数据是板块主力资金，不是个股资金")
    if not boards:
        warnings.append("没有板块通过当前条件；请核对RPS、资金覆盖和报告日期，不代表全市场没有机会")
    members = {}
    skipped = {}
    deadline = time.monotonic() + 90
    for board in boards:
        if cancelled(): break
        if time.monotonic() > deadline:
            warnings.append("成员查询达到等待上限，未取得的板块已跳过")
            break
        progress(f"正在读取板块成员：{board['sector_name']}")
        try:
            result = fetch_members(board, report["sector_type"], report["dataset_id"])
            for member in result["rows"]:
                code = str(member.get("code", ""))
                if code not in file_map or member.get("security_type") == "ETF": continue
                entry = members.setdefault(code, {"name": member.get("name", code), "boards": []})
                if entry["name"] == code and member.get("name") not in (None, code): entry["name"] = member["name"]
                if not any(b["sector_code"] == board["sector_code"] for b in entry["boards"]):
                    entry["boards"].append({**board, "member_source": result["source"]})
        except Exception as exc:
            warnings.append(f"{board['sector_name']}成员不可用：{type(exc).__name__}，已跳过")
    names_missing = [code for code, item in members.items() if not item["name"] or item["name"] == code]
    if names_missing:
        try:
            names = name_fetcher(names_missing)
            for code in names_missing: members[code]["name"] = names.get(code, code)
        except Exception:
            warnings.append("证券名称读取失败，无法核对风险名称的股票已剔除")
    rows = []
    for index, (code, member) in enumerate(members.items()):
        if cancelled(): break
        if index % 25 == 0: progress(f"正在验证个股日线：{index}/{len(members)}")
        name = member["name"]
        reason = None
        if not name or name == code or re.search(r"ST|退|ETF", str(name), re.I):
            reason = "名称未知、风险名称或非股票"
        try:
            metrics, reason = (None, reason) if reason else stock_metrics(fs._read_daily(file_map[code]), report["report_date"], options["min_amount"])
            if metrics and options.get("formula"):
                bars = fs._aggregate_weekly(metrics["bars"]) if options.get("timeframe") == "weekly" else metrics["bars"]
                if fs._evaluate_custom_formula(bars, options["formula"], require_true=True) is None:
                    metrics, reason = None, "所选公式未通过或指标不足"
        except Exception:
            metrics, reason = None, "日线读取或公式执行失败"
        if not metrics:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        best = max(member["boards"], key=lambda b: b["board_score"])
        metrics.pop("bars")
        evidence = {
            "board": {"板块": best["sector_name"], "分组": best["tier"], "排名": best["rank"], "RPS": best["rps50"]},
            "trend": {"收盘": metrics["close"], "均线20": metrics["ma20"], "均线60": metrics["ma60"], "二十日涨幅": metrics["return_20d"]},
            "liquidity": {"成交额": metrics["amount"], "量比": metrics["volume_ratio"]},
            "flow": {"三日板块主力净流入": best.get("three_day_main_net_inflow"), "完整天数": best.get("three_day_flow_available_days"), "匹配": best.get("three_day_flow_match")},
            "market": report.get("trend_summary", {}),
        }
        if options.get("formula"):
            evidence["formula"] = {"技术公式": options.get("formula_name", "已选技术公式"), "周期": options.get("timeframe", "daily"), "结果": "通过；不包含财务核验"}
        risks = ["财务、公告及次日涨跌停/可成交性未核验；规则得分不是上涨概率", "通达信日线未在此核验复权口径，除权附近的技术指标需复核"]
        if best.get("three_day_flow_status") != "complete": risks.append("板块资金历史不足，未提供资金支持加分；缺失不是零")
        elif best.get("three_day_main_net_inflow", 0) <= 0: risks.append("板块三日主力净流入非正，不属于资金支持信号")
        if best["flow_reference"]: risks.append("资金来自东方财富唯一同名板块，仅作参考，成分可能不同")
        if metrics["volume_ratio"] is None: risks.append("量比缺失，未提供量比加分")
        rows.append({"code": code, "name": name, "data_date": report["report_date"], "score": best["board_score"] + metrics["stock_score"], "board_score": best["board_score"], **metrics,
                     "boards": [{"code": b["sector_code"], "name": b["sector_name"], "source": b["member_source"]} for b in member["boards"]],
                     "evidence": evidence, "reasons": ["所属板块通过RPS和排名/改善条件", "个股收盘高于均线且中期涨幅为正", "成交额通过设置的流动性门槛"] + (["通过所选技术公式"] if options.get("formula") else []),
                     "risks": risks, "ai": {"status": "pending" if options["ai_enabled"] else "disabled"}})
    rows.sort(key=lambda r: (-r["score"], -r["amount"], r["code"]))
    if skipped.get("个股日期与报告不一致"):
        warnings.append(f"有{skipped['个股日期与报告不一致']}只个股日线未覆盖报告日{report['report_date']}；请在通达信下载该日及之前的个股日线，再重新生成候选。旧日线不会冒充最新数据。")
    rows = rows[:options["max_results"]]
    for index, row in enumerate(rows): row["rank"] = index + 1
    return {"rule_version": RULE_VERSION, "report_date": report["report_date"], "provider": report["dataset_id"], "sector_type": report["sector_type"], "rows": rows, "boards": [{"code": b["sector_code"], "name": b["sector_name"], "score": b["board_score"]} for b in boards],
            "skipped": skipped, "warnings": warnings, "disclaimer": DISCLAIMER, "rules": "板块最高55分；相关排名改善只取最大值。个股最高45分。分数仅用于规则排序，无胜率含义。未通过条件或缺失不作通过。"}


def explanation_key(row, model):
    return fingerprint({"version": PROMPT_VERSION, "model": model, "row": {k: v for k, v in row.items() if k not in {"ai", "rank"}}})


def validate_explanation(text, row):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {"code", "reasons", "risks", "observe"} or value["code"] != row["code"]:
        raise ValueError("AI解读结构或股票代码不符合要求")
    for key in ("reasons", "risks", "observe"):
        items = value[key]
        if not isinstance(items, list) or not 1 <= len(items) <= 3: raise ValueError("AI解读条数不符合要求")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "evidence_ids"}: raise ValueError("AI解读缺少证据引用")
            content, refs = item["text"], item["evidence_ids"]
            if not isinstance(content, str) or not 1 <= len(content) <= 180 or re.search(r"[0-9]|保证|稳赚|必涨|胜率|买入|卖出|加仓|目标价|涨停|梭哈", content):
                raise ValueError("AI解读含未经允许的数字、交易指令或收益承诺")
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in row["evidence"] for ref in refs):
                raise ValueError("AI解读引用了不存在的证据")
    return value


class CandidateService:
    """Local persisted snapshots; bounded background work and cached AI."""
    def __init__(self, database, report_builder, member_fetcher=strict_members, name_fetcher=fs._enrich_names):
        self.database, self.report_builder, self.member_fetcher = database, report_builder, member_fetcher
        self.name_fetcher = name_fetcher
        self.lock = Lock()
        self.active = set()

    def _save(self, run):
        with self.database.connection() as conn:
            conn.execute("UPDATE candidate_runs SET state_json=?,updated_at=? WHERE id=?", (json.dumps(run, ensure_ascii=False, allow_nan=False), time.time(), run["id"]))

    def get(self, run_id):
        with self.database.connection() as conn:
            row = conn.execute("SELECT state_json,updated_at,cancel_requested FROM candidate_runs WHERE id=?", (run_id,)).fetchone()
        if not row: return None
        run = json.loads(row["state_json"])
        if run["status"] in {"generating", "interpreting"} and row["updated_at"] < time.time()-600:
            run.update(status="interrupted", message="上次任务已中断，已生成候选仍保留；可重试解读，不会自动重复收费")
            self._save(run)
        return run

    def latest(self, provider, sector_type):
        with self.database.connection() as conn:
            row = conn.execute("SELECT id FROM candidate_runs WHERE provider=? AND sector_type=? ORDER BY created_at DESC LIMIT 1", (provider, sector_type)).fetchone()
        return self.get(row[0]) if row else None

    def cancelled(self, run_id):
        with self.database.connection() as conn:
            row = conn.execute("SELECT cancel_requested FROM candidate_runs WHERE id=?", (run_id,)).fetchone()
        return not row or bool(row[0])

    def cancel(self, run_id):
        with self.database.connection() as conn:
            conn.execute("UPDATE candidate_runs SET cancel_requested=1 WHERE id=?", (run_id,))

    def start(self, options, client_factory):
        if options.get("formula"):
            errors = fs.validate_formula(options["formula"])
            if errors: raise ValueError("公式无法执行：" + "；".join(errors))
            if uses_finance(options["formula"]): raise ValueError("候选池的可选公式只支持技术条件；含FINANCE的公式请使用原有公式选股功能，不能把未知财务判为通过")
        request_key = fingerprint(options)
        with self.lock:
            with self.database.connection() as conn:
                # Serialize across desktop windows/processes sharing this DB.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT state_json,updated_at FROM candidate_runs WHERE request_key=? ORDER BY created_at DESC LIMIT 1", (request_key,)).fetchone()
                if existing:
                    previous = json.loads(existing[0])
                    if previous["status"] in {"generating", "interpreting"} and existing[1] >= time.time()-600:
                        return previous
                run = {"id": uuid.uuid4().hex, "status": "generating", "message": "正在读取已有板块报告…", "options": options, "result": None}
                conn.execute("INSERT INTO candidate_runs(id,request_key,provider,sector_type,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (run["id"], request_key, options["provider"], options["sector_type"], json.dumps(run, ensure_ascii=False), time.time(), time.time()))
            self.active.add(run["id"])
            Thread(target=self._generate, args=(run, client_factory), daemon=True).start()
        return run

    def _generate(self, run, client_factory):
        def progress(message):
            run["message"] = message
            self._save(run)
        try:
            options = run["options"]
            report = self.report_builder(options["sector_type"], None, options["provider"])
            run["result"] = generate_candidates(self.database, report, options, self.member_fetcher, progress, lambda: self.cancelled(run["id"]), self.name_fetcher)
            run["result"]["saved_at"] = shanghai_now().isoformat()
            run.update(status="interpreting" if options["ai_enabled"] and run["result"]["rows"] else "completed", message="候选已生成，正在逐只加载AI解读…")
            self._save(run)  # Results become visible before any model request.
            if options["ai_enabled"]:
                self._interpret(run, client_factory, [r["code"] for r in run["result"]["rows"]])
            else:
                run["message"] = "候选生成完成（未调用AI）"
            if self.cancelled(run["id"]): run.update(status="cancelled", message="已取消后续任务；已生成结果保留，正在执行的请求可能仍产生费用")
            self._save(run)
        except Exception as exc:
            run.update(status="failed", message=str(exc)[:500])
            self._save(run)
        finally:
            with self.lock: self.active.discard(run["id"])

    def _interpret(self, run, client_factory, codes):
        options = run["options"]
        client = None
        for row in run["result"]["rows"]:
            if row["code"] not in codes or self.cancelled(run["id"]): continue
            cache_key = explanation_key(row, options["model"])
            with self.database.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cached = conn.execute("SELECT content_json FROM candidate_ai_cache WHERE cache_key=?", (cache_key,)).fetchone()
                acquired = False
                if not cached:
                    conn.execute("DELETE FROM candidate_ai_leases WHERE cache_key=? AND expires_at<?", (cache_key, time.time()))
                    acquired = conn.execute("INSERT OR IGNORE INTO candidate_ai_leases(cache_key,owner,expires_at) VALUES(?,?,?)", (cache_key, run["id"], time.time()+120)).rowcount == 1
            if cached:
                row["ai"] = {"status": "completed", "cached": True, "model": options["model"], "content": json.loads(cached[0])}
                self._save(run)
                continue
            if not acquired:
                row["ai"] = {"status": "failed", "message": "其他任务正在解读相同事实，未重复调用；稍后重试可复用成功缓存"}
                self._save(run)
                continue
            row["ai"] = {"status": "loading"}
            self._save(run)
            try:
                if client is None: client = client_factory(options["model"])
                text = client.generate_candidate_explanation(json.dumps({"code": row["code"], "name": row["name"], "date": row["data_date"], "facts": row["evidence"], "program_risks": row["risks"], "rules": run["result"]["rules"]}, ensure_ascii=False))
                if client.last_finish_reason == "length": raise ValueError("AI解读被截断，请重试")
                content = validate_explanation(text, row)
                with self.database.connection() as conn:
                    conn.execute("INSERT OR REPLACE INTO candidate_ai_cache(cache_key,content_json,created_at) VALUES(?,?,?)", (cache_key, json.dumps(content, ensure_ascii=False), time.time()))
                row["ai"] = {"status": "completed", "cached": False, "model": options["model"], "content": content}
            except Exception:
                row["ai"] = {"status": "failed", "message": "AI未配置、网络失败或解读未通过证据检查；程序候选不受影响，可重试"}
            finally:
                with self.database.connection() as conn:
                    conn.execute("DELETE FROM candidate_ai_leases WHERE cache_key=? AND owner=?", (cache_key, run["id"]))
            self._save(run)
        failed = any(r["ai"]["status"] == "failed" for r in run["result"]["rows"])
        run.update(status="partial" if failed else "completed", message="候选已保留；部分AI解读失败，可逐只重试" if failed else "候选及解读完成；解读仅供核对，不能代替风险检查")

    def retry(self, run_id, code, client_factory, model):
        with self.lock:
            run = self.get(run_id)
            if not run or not run.get("result") or code not in [r["code"] for r in run["result"]["rows"]]: raise ValueError("候选股票不存在")
            if run_id in self.active or run["status"] in {"generating", "interpreting"}: raise ValueError("该任务仍在执行，请勿重复解读")
            run["options"]["model"] = model
            run.update(status="interpreting", message="正在重试对应股票的AI解读…")
            with self.database.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = json.loads(conn.execute("SELECT state_json FROM candidate_runs WHERE id=?",(run_id,)).fetchone()[0])
                if previous["status"] in {"generating","interpreting"}: raise ValueError("其他窗口正在解读此记录，请勿重复调用")
                conn.execute("UPDATE candidate_runs SET cancel_requested=0,state_json=?,updated_at=? WHERE id=?", (json.dumps(run,ensure_ascii=False),time.time(),run_id))
            self.active.add(run_id)
            def worker():
                try:
                    self._interpret(run, client_factory, [code])
                    if self.cancelled(run_id): run.update(status="cancelled", message="后续任务已取消，已生成解读保留")
                    self._save(run)
                except Exception:
                    run.update(status="partial", message="解读任务中断；已生成候选与解读保留，可稍后重试")
                    self._save(run)
                finally:
                    with self.lock: self.active.discard(run_id)
            Thread(target=worker, daemon=True).start()
        return run
