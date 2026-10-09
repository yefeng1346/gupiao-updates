"""Configurable screens use exact trading windows and never alter archives."""
from datetime import date
import importlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app import capital_flow as flow
from app.db import Database
from app.flow_calendar import trading_window
from app.flow_archive import update_daily, sync_feed
from app.flow_import import import_flow, imported_flow_report, TEMPLATE
from app.flow_tasks import FlowHistoryJobs
from test_capital_flow_api import LocalAsgiClient


class FlowRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"rule.db"
        self.db = Database(self.path)
        self.days = trading_window(date(2026,9,30),60)
        self.codes = ["BK0000","BK0001","BK0002"]
        self.db.save_sector_capital_flow_catalog("concept",[
            {"sector_code":code,"sector_name":f"样本{i}"} for i,code in enumerate(self.codes)])
        self.db.upsert_sector_capital_flow_daily([
            {"sector_type":"concept","sector_code":code,"sector_name":f"样本{i}",
             "trade_date":day,"main_net_inflow":100 if index%2==1 else 0 if i==2 else -1}
            for index,day in enumerate(self.days) for i,code in enumerate(self.codes)
            if not (i==1 and day==self.days[-3])])

    def query(self,n=10,m=6):
        return flow.fetch_sector_capital_flow_report("concept",1000,"2026-09-30",self.db,
                local_only=True,window_days=n,min_inflow_days=m)

    def test_shorter_window_threshold_and_amount_are_recomputed_without_network(self):
        with patch.object(flow,"fetch_sector_capital_flow") as net,patch.object(flow,"_fetch_board_flow_history") as history:
            self.assertEqual(self.query()["inflow_days_rank"],[])
            report = self.query(5,3)
            net.assert_not_called(); history.assert_not_called()
        self.assertEqual([row["sector_code"] for row in report["inflow_days_rank"]],["BK0002","BK0000"])
        self.assertEqual(report["window_dates"],self.days[-5:])
        self.assertEqual(report["window_days"],5)
        self.assertEqual(report["min_inflow_days"],3)
        self.assertEqual(report["inflow_days_rank"][1]["window_main_net_inflow"],298)
        self.assertEqual(report["inflow_days_rank"][1]["positive_flow_days"],3)
        sample = report["history_coverage"]["incomplete_samples"][0]
        self.assertEqual((sample["available_days"],sample["expected_days"]),(4,5))
        self.assertEqual(len(self.db.get_sector_capital_flow_history("concept","2026-09-30",60)),179)

    def test_one_day_and_max_window_boundary(self):
        one = self.query(1,1)
        self.assertEqual(len(one["inflow_days_rank"]),3)
        self.assertEqual(one["history_coverage"]["complete_boards"],3)
        maximum = self.query(60,30)
        self.assertEqual(len(maximum["window_dates"]),60)
        self.assertEqual(len(maximum["inflow_days_rank"]),2)
        self.assertEqual(self.query(60,31)["inflow_days_rank"],[])

    def test_missing_dates_not_filled_by_older_dates_and_zero_not_positive(self):
        report = self.query(3,2)
        self.assertEqual([r["sector_code"] for r in report["inflow_days_rank"]],["BK0002","BK0000"])
        self.assertEqual(self.query(3,3)["inflow_days_rank"],[])
        self.assertEqual(report["history_coverage"]["complete_boards"],2)
        self.assertEqual(report["history_coverage"]["incomplete_samples"][0]["available_days"],2)

    def test_rule_validation_rejects_invalid_numbers(self):
        for n,m in ((0,1),(61,1),(5,6),(5,0),(5,2.5),(True,1),(10,"6"),(5.0,3)):
            with self.subTest(n=n,m=m),self.assertRaises(ValueError): self.query(n,m)

    def test_reopen_rebuilds_new_rule_not_saved_old_screen(self):
        self.query(5,3)
        self.db = Database(self.path)
        report = self.query(10,5)
        self.assertEqual(report["window_days"],10)
        self.assertEqual(report["inflow_days_rank"][0]["positive_flow_days"],5)
        self.assertEqual(len(report["inflow_days_rank"][0]["flow_sequence"]),10)

    def test_legacy_snapshot_does_not_claim_custom_rule_matches(self):
        old = {"rows":[{"sector_code":"BK1"}],"window_dates":self.days[-10:],
               "inflow_days_rank":[{"sector_code":"BK1"}],"history_coverage":{"complete_boards":1,"window_days":10}}
        unchanged = flow.flow_report_with_rule(old,10,6)
        self.assertEqual(len(unchanged["inflow_days_rank"]),1)
        changed = flow.flow_report_with_rule(old,5,3)
        self.assertEqual(changed["inflow_days_rank"],[])
        self.assertEqual(changed["window_dates"],[])
        self.assertEqual(len(changed["rows"]),1)
        self.assertFalse(changed["window_complete"])
        self.assertEqual(len(old["inflow_days_rank"]),1)

    def test_import_sources_support_custom_rule_without_mixing(self):
        days = self.days[-5:]
        text = TEMPLATE + "".join(f"{day},880501,导入样本,{100 if i<3 else -1},1,2\n" for i,day in enumerate(days))
        import_flow(self.db,"tdx_import",text,"concept")
        import_flow(self.db,"ths_import",text.replace(",100,",",-100,"),"concept")
        tdx = imported_flow_report(self.db,"tdx_import","concept","2026-09-30",50,window_days=5,min_inflow_days=3)
        ths = imported_flow_report(self.db,"ths_import","concept","2026-09-30",50,window_days=5,min_inflow_days=3)
        self.assertEqual(tdx["inflow_days_rank"][0]["positive_flow_days"],3)
        self.assertEqual(ths["inflow_days_rank"],[])
        self.assertEqual(tdx["source_id"],"tdx_import")

    def test_daily_update_keeps_custom_rule_and_empty_report_shape(self):
        with patch("app.flow_archive.collect_current",side_effect=RuntimeError("offline")):
            report = update_daily(self.db,"concept","2026-09-30",window_days=5,min_inflow_days=3)
            self.assertEqual(len(report["inflow_days_rank"]),2)
            empty = update_daily(self.db,"industry","2026-09-30",window_days=3,min_inflow_days=1)
            self.assertEqual((empty["window_days"],empty["min_inflow_days"]),(3,1))

    def test_job_checkpoints_snapshot_and_recovery_keep_rule(self):
        jobs = FlowHistoryJobs(); self.addCleanup(jobs.shutdown)
        with patch("app.flow_archive.collect_current",side_effect=RuntimeError("offline")):
            job = jobs.start(self.db,"concept","2026-09-30",lambda fn:fn,mode="daily",window_days=5,min_inflow_days=3)
            jobs.workers[job["id"]].join(timeout=5)
        saved = FlowHistoryJobs().snapshot(job["id"],Database(self.path))
        self.assertEqual((saved["window_days"],saved["min_inflow_days"]),(5,3))
        self.assertEqual(len(saved["result"]["inflow_days_rank"]),2)
        resumed = {"sector_type":"industry","date":"2026-09-30","mode":"daily","window_days":3,"min_inflow_days":1}
        with patch.object(jobs,"start",return_value={}) as start,patch.object(self.db,"pending_flow_jobs",return_value=[resumed]):
            jobs.resume_pending(self.db,lambda fn:fn)
            self.assertEqual(start.call_args.kwargs["window_days"],3)
            self.assertEqual(start.call_args.kwargs["min_inflow_days"],1)

    def test_atomic_lease_rejects_different_rule_without_replacing_job(self):
        state = {"id":"old","status":"running","sector_type":"concept","date":"2026-09-30",
                 "window_days":5,"min_inflow_days":3,"expires_at":time.time()+1000,"round":0}
        self.db.claim_flow_job(state,"owner")
        with self.assertRaises(ValueError):
            self.db.claim_flow_job({**state,"id":"new","window_days":10},"owner")
        self.assertEqual(self.db.get_flow_job("old")["window_days"],5)

    def test_feed_sync_respects_longer_window(self):
        from test_flow_archive import current
        from tools.collect_daily_flow import export_daily
        day = self.days[-20]
        report = flow.share_current_flow_with_history(current(day),self.db)
        entry = export_daily(report,Path(self.temp.name)/"export",["https://mirror.example"])
        payload = (Path(self.temp.name)/"export"/day/"concept.json").read_bytes()
        index = json.dumps({"schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY",
                            "redistribution_authorized":True,"entries":[entry]}).encode()
        with patch("app.flow_archive._read",return_value=index) as read:
            self.assertEqual(sync_feed(self.db,"concept","2026-09-30",["https://index.example"],window_days=10)["saved_days"],[])
            self.assertEqual(read.call_count,1)
        with patch("app.flow_archive._read",side_effect=[index,payload]):
            self.assertEqual(sync_feed(self.db,"concept","2026-09-30",["https://index.example"],window_days=30)["saved_days"],[day])


class FlowRuleApiTests(unittest.TestCase):
    setUp = FlowRuleTests.setUp
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as temp:
            with patch("app.db.Database",return_value=Database(Path(temp)/"bootstrap.db")),patch("app.config.ensure_directories"):
                cls.main = importlib.import_module("main")

    def test_api_rule_recalculates_cache_and_rejects_bad_input(self):
        with patch.object(self.main,"database",self.db),patch.object(flow,"fetch_sector_capital_flow") as net:
            client = LocalAsgiClient(self.main.app)
            for endpoint in ("/api/sector-capital-flow","/api/sector-capital-flow/cache"):
                result = client.get(endpoint,{"date":"2026-09-30","window_days":5,"min_inflow_days":3})
                self.assertEqual(result.status_code,200)
                self.assertEqual(len(result.json()["inflow_days_rank"]),2)
                self.assertEqual(client.get(endpoint,{"window_days":3,"min_inflow_days":4}).status_code,400)
                self.assertEqual(client.get(endpoint,{"window_days":61}).status_code,422)
            net.assert_not_called()

    def test_api_legacy_fallback_does_not_return_other_rule(self):
        self.db.clear_capital_flow_data()
        self.db.save_sector_capital_flow_report({"sector_type":"concept","requested_date":"2026-09-30",
            "report_date":"2026-09-30","total":1,"rows":[{"sector_code":"BK1"}],"inflow_days_rank":[{"sector_code":"BK1"}]})
        with patch.object(self.main,"database",self.db):
            for endpoint in ("/api/sector-capital-flow","/api/sector-capital-flow/cache"):
                result = LocalAsgiClient(self.main.app).get(endpoint,{"date":"2026-09-30","window_days":5,"min_inflow_days":3})
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json()["inflow_days_rank"],[])
                self.assertEqual(result.json()["window_days"],5)

    def test_job_api_passes_valid_rule_and_rejects_invalid_before_start(self):
        client = LocalAsgiClient(self.main.app)
        body = {"sector_type":"concept","date":"2026-09-30","window_days":5,"min_inflow_days":3}
        with patch.object(self.main,"database",self.db),patch.object(self.main._flow_jobs,"start",return_value={"ok":True}) as start:
            result = client.get("/api/sector-capital-flow/history/jobs",method="POST",body=body)
            self.assertEqual(result.status_code,200)
            self.assertEqual(start.call_args.kwargs["window_days"],5)
            self.assertEqual(start.call_args.kwargs["min_inflow_days"],3)
            start.reset_mock()
            invalid = {**body,"window_days":2,"min_inflow_days":3}
            # Cross-field checking happens in the real task entry, before SQL/network.
            start.side_effect = lambda *args,**kwargs: flow.validate_flow_rule(kwargs["window_days"],kwargs["min_inflow_days"])
            self.assertEqual(client.get("/api/sector-capital-flow/history/jobs",method="POST",body=invalid).status_code,400)
            start.reset_mock()
            self.assertEqual(client.get("/api/sector-capital-flow/history/jobs",method="POST",body={**body,"window_days":5.5}).status_code,422)
            start.assert_not_called()


if __name__ == "__main__": unittest.main()
