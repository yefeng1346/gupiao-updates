from datetime import date, datetime
import importlib
import json
from pathlib import Path
import tempfile
import time
from threading import Event
import unittest
from unittest.mock import patch, Mock

from app.candidates import CandidateService, generate_candidates, stock_metrics, validate_explanation
from app.db import Database
from app.flow_calendar import trading_window


def explanation(code="600001"):
    return json.dumps({"code":code,"reasons":[{"text":"板块与个股趋势提供候选依据，仍需复核","evidence_ids":["board","trend"]}],
                       "risks":[{"text":"板块资金不代表个股资金，财务及公告尚待核验","evidence_ids":["flow"]}],
                       "observe":[{"text":"继续观察量价是否配合，不能据此作确定性预测","evidence_ids":["liquidity"]}]},ensure_ascii=False)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.db = Database(Path(self.folder.name)/"test.db")
        self.days = trading_window(date(2026,10,9),100)
        self.bars = [{"date":date.fromisoformat(day),"close":10+i*.05,"high":11+i*.05,"low":9+i*.05,"volume":10000,"amount":30000000} for i,day in enumerate(self.days)]
        self.options = {"provider":"akshare","sector_type":"concept","board_limit":5,"max_results":10,"min_amount":20000000,"require_flow":False,"ai_enabled":True,"model":"test-model","formula":None,"timeframe":"daily"}
        self.report = {"report_date":"2026-10-09","sector_type":"concept","dataset_id":"akshare",
                       "latest_top100":[{"sector_code":"BK0001","sector_name":"模拟板块","rank":5,"rps50":90,"rank_change_5d":20}],
                       "t_groups":{"T0":[{"sector_code":"BK0001","sector_name":"模拟板块","rank":5,"rps50":90,"rank_change_5d":20}]},"modules":{},"trend_summary":{"headline":"模拟市场"}}
        self.db.save_sector_capital_flow_catalog("concept", [{"sector_code":"BK0001","sector_name":"模拟板块"}])
        self.db.upsert_sector_capital_flow_daily([{"sector_type":"concept","sector_code":"BK0001","sector_name":"模拟板块","trade_date":day,"main_net_inflow":100} for day in self.days[-3:]])
        self.members = Mock(return_value={"source":"模拟成员","rows":[{"code":"600001","name":"模拟股票"},{"code":"600001","name":"模拟股票"},{"code":"600002","name":"ST样本"}]})
        for target,value in (("app.candidates.shanghai_now",datetime.fromisoformat("2026-10-10T16:00:00+08:00")),("app.flow_summary.shanghai_now",datetime.fromisoformat("2026-10-10T16:00:00+08:00")),("app.formula_screen._discover_day_files",[("600001",Path("fake.day")),("600002",Path("bad.day"))]),("app.formula_screen._read_daily",self.bars)):
            p = patch(target,return_value=value); p.start(); self.addCleanup(p.stop)
        self.client = Mock(last_finish_reason="stop")
        self.client.generate_candidate_explanation.return_value = explanation()

    def generate(self,**options):
        return generate_candidates(self.db,self.report,{**self.options,**options},self.members)

    def wait(self,service,run_id):
        for _ in range(200):
            run=service.get(run_id)
            if run["status"] not in {"generating","interpreting"} and run_id not in service.active: return run
            time.sleep(.01)
        self.fail("Background job did not finish")

    def service(self):
        return CandidateService(self.db,lambda *args:self.report,self.members)

    def test_deterministic_deduplicated_scored_and_risk_filtered(self):
        result=self.generate()
        self.assertEqual(len(result["rows"]),1)
        row=result["rows"][0]
        self.assertEqual(row["score"],95)
        self.assertEqual(len(row["boards"]),1)
        self.assertEqual(row["evidence"]["flow"]["三日板块主力净流入"],300)
        self.assertEqual(result["skipped"]["名称未知、风险名称或非股票"],1)
        self.assertEqual(self.generate()["rows"],result["rows"])

    def test_missing_flow_is_not_zero_and_strict_gate_excludes(self):
        with self.db.connection() as conn: conn.execute("DELETE FROM sector_capital_flow_daily WHERE trade_date=?",(self.days[-2],))
        row=self.generate()["rows"][0]
        self.assertIsNone(row["evidence"]["flow"]["三日板块主力净流入"])
        self.assertEqual(row["board_score"],40)
        self.assertEqual(self.generate(require_flow=True)["rows"],[])

    def test_no_future_bars_and_date_mismatch(self):
        future={**self.bars[-1],"date":date(2026,10,12),"close":9999}
        metrics,_=stock_metrics(self.bars+[future],"2026-10-09",20000000)
        self.assertEqual(metrics["close"],self.bars[-1]["close"])
        self.assertEqual(stock_metrics(self.bars[:-1],"2026-10-09",20000000)[1],"个股日期与报告不一致")
        self.report["report_date"]="2026-09-30"
        with self.assertRaisesRegex(ValueError,"最近已收盘"): self.generate()

    def test_insufficient_history_liquidity_and_formula(self):
        self.assertEqual(stock_metrics(self.bars[:40],"2026-10-09",20000000)[1],"历史不足")
        self.assertIsNone(stock_metrics(self.bars,"2026-10-09",50000000)[0])
        self.assertEqual(self.generate(formula="选股:C<0;")["rows"],[])
        self.assertEqual(len(self.generate(formula="选股:C>MA(C,20);")["rows"]),1)

    def test_missing_day_files_is_actionable(self):
        with patch("app.formula_screen._discover_day_files",return_value=[]):
            with self.assertRaisesRegex(ValueError,"通达信个股日线"): self.generate()

    def test_one_board_failure_preserves_other_board(self):
        self.report["latest_top100"].append({"sector_code":"BK0002","sector_name":"失败板块","rank":6,"rps50":89})
        self.members.side_effect=lambda board,*args: (_ for _ in ()).throw(RuntimeError("down")) if board["sector_code"]=="BK0002" else {"source":"模拟","rows":[{"code":"600001","name":"模拟股票"}]}
        result=self.generate()
        self.assertEqual(len(result["rows"]),1)
        self.assertTrue(any("失败板块成员不可用" in w for w in result["warnings"]))

    def test_ai_cannot_add_codes_numbers_or_missing_evidence(self):
        row=self.generate()["rows"][0]
        self.assertEqual(validate_explanation(explanation(),row)["code"],"600001")
        for text in (explanation("600002"),explanation().replace("板块与个股趋势","上涨90%"),explanation().replace('"board"','"invented"'),explanation().replace("提供候选依据","保证必涨")):
            with self.assertRaises((ValueError,TypeError)): validate_explanation(text,row)

    def test_results_visible_before_ai_and_duplicate_jobs_reused(self):
        entered,release=Event(),Event()
        self.client.generate_candidate_explanation.side_effect=lambda text: (entered.set(),release.wait(2),explanation())[2]
        service=self.service(); factory=lambda _:self.client
        run=service.start(self.options,factory)
        self.assertTrue(entered.wait(2))
        self.assertTrue(service.get(run["id"])["result"]["rows"])
        other=self.service()
        self.assertEqual(other.start(self.options,factory)["id"],run["id"])
        release.set()
        self.assertEqual(self.wait(service,run["id"])["status"],"completed")
        self.assertEqual(self.client.generate_candidate_explanation.call_count,1)

    def test_successful_ai_cached_on_regenerate_and_restored_without_calls(self):
        service=self.service(); factory=lambda _:self.client
        first=self.wait(service,service.start(self.options,factory)["id"])
        self.assertFalse(first["result"]["rows"][0]["ai"]["cached"])
        second=self.wait(service,service.start(self.options,factory)["id"])
        self.assertTrue(second["result"]["rows"][0]["ai"]["cached"])
        self.assertEqual(self.client.generate_candidate_explanation.call_count,1)
        self.assertEqual(self.service().latest("akshare","concept")["id"],second["id"])
        self.assertEqual(self.client.generate_candidate_explanation.call_count,1)

    def test_different_jobs_share_ai_lease_and_retry_cache(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        self.client.generate_candidate_explanation.side_effect = lambda text: (entered.set(), release.wait(4), explanation())[2]
        first_service = self.service()
        first = first_service.start(self.options, lambda _: self.client)
        self.assertTrue(entered.wait(2))
        second_service = self.service()
        second = self.wait(second_service, second_service.start({**self.options,"max_results":5},lambda _:self.client)["id"])
        self.assertEqual(second["status"],"partial")
        self.assertEqual(self.client.generate_candidate_explanation.call_count,1)
        release.set()
        self.wait(first_service,first["id"])
        for _ in range(100):
            if second["id"] not in second_service.active: break
            time.sleep(.01)
        second_service.retry(second["id"],"600001",lambda _:self.client,"test-model")
        complete = self.wait(second_service,second["id"])
        self.assertTrue(complete["result"]["rows"][0]["ai"]["cached"])
        self.assertEqual(self.client.generate_candidate_explanation.call_count,1)

    def test_candidate_prompt_uses_existing_client_without_changing_review(self):
        from app.llm import ArkClient, SYSTEM_PROMPT
        client = ArkClient("test-only","https://example.invalid","test-model")
        with patch.object(client,"_complete",return_value=explanation()) as complete:
            client.generate_candidate_explanation('{"code":"600001"}')
            self.assertIn("不得新增股票",complete.call_args.args[0])
            self.assertIn("600001",complete.call_args.args[1])
            client.generate_review("mock report")
            self.assertEqual(complete.call_args.args[0],SYSTEM_PROMPT)

    def test_ai_failure_preserves_candidate_and_retry_recovers(self):
        self.client.generate_candidate_explanation.side_effect=RuntimeError("model down")
        service=self.service(); factory=lambda _:self.client
        run=self.wait(service,service.start(self.options,factory)["id"])
        self.assertEqual(run["status"],"partial")
        self.assertEqual(run["result"]["rows"][0]["code"],"600001")
        self.client.generate_candidate_explanation.side_effect=None
        service.retry(run["id"],"600001",factory,"test-model")
        self.assertEqual(self.wait(service,run["id"])["status"],"completed")

    def test_disabled_ai_does_not_construct_client(self):
        factory=Mock(side_effect=AssertionError("must not call model"))
        service=self.service(); run=self.wait(service,service.start({**self.options,"ai_enabled":False},factory)["id"])
        self.assertEqual(run["result"]["rows"][0]["ai"]["status"],"disabled")
        factory.assert_not_called()

    def test_finance_formula_rejected_and_no_ai_charge(self):
        factory=Mock()
        with self.assertRaisesRegex(ValueError,"FINANCE"): self.service().start({**self.options,"formula":"选股:FINANCE(25)>0;"},factory)
        factory.assert_not_called()

    def test_cancel_keeps_generated_rows(self):
        entered,release=Event(),Event()
        self.client.generate_candidate_explanation.side_effect=lambda text:(entered.set(),release.wait(2),explanation())[2]
        service=self.service(); run=service.start(self.options,lambda _:self.client)
        self.assertTrue(entered.wait(2)); service.cancel(run["id"]); release.set()
        completed=self.wait(service,run["id"])
        self.assertEqual(completed["status"],"cancelled")
        self.assertEqual(len(completed["result"]["rows"]),1)

    def test_api_limits_restore_retry_and_persistence(self):
        from test_capital_flow_api import LocalAsgiClient
        with patch("app.db.Database",return_value=self.db),patch("app.config.ensure_directories"):
            main=importlib.import_module("main")
        service=self.service()
        with patch.object(main,"database",self.db),patch.object(main,"candidate_service",return_value=service),patch.object(main,"candidate_llm_client",return_value=self.client):
            client=LocalAsgiClient(main.app)
            response=client.get("/api/candidates/generate",method="POST",body={"provider":"akshare","ai_enabled":False,"max_results":11})
            self.assertEqual(response.status_code,422)
            response=client.get("/api/candidates/generate",method="POST",body={"provider":"akshare","ai_enabled":False})
            self.assertEqual(response.status_code,200)
            run=self.wait(service,response.json()["id"])
            restored=client.get("/api/candidates/latest",params={"provider":"akshare"}).json()["run"]
            self.assertEqual(restored["id"],run["id"])
            self.assertEqual(client.get(f"/api/candidates/runs/{run['id']}").status_code,200)
            self.assertEqual(client.get("/api/candidates/runs/missing").status_code,404)
            self.assertEqual(client.get(f"/api/candidates/runs/{run['id']}/interpret",method="POST",body={"code":"600009"}).status_code,409)


if __name__=="__main__": unittest.main()
