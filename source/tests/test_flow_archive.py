from datetime import date, datetime
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from app.db import Database
from app.flow_calendar import confirmed_close_date, trading_window
from app.capital_flow import share_current_flow_with_history, fetch_sector_capital_flow_report, fetch_sector_capital_flow
from app.flow_archive import DailyCollector, sync_feed, update_daily, validate_daily
from app.flow_tasks import FlowHistoryJobs
from tools.collect_daily_flow import export_daily


def current(day="2026-09-30", value=100, capture="16:00:00", source="15:00:00", count=2):
    return {"ok":True,"sector_type":"concept","total":count,"universe_complete":True,
            "captured_at":f"{day}T{capture}+08:00","updated_at":f"{day}T{source}+08:00",
            "rows":[{"sector_code":f"BK{i:04}","sector_name":f"测试{i}","main_net_inflow":value,
                     "source_time":f"{day}T{source}+08:00"} for i in range(count)]}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"test.db"; self.db = Database(self.path)

    def test_1500_requires_actual_post_close_capture_not_evening_restart(self):
        now = datetime.fromisoformat("2026-09-30T20:00:00+08:00")
        stamp = "2026-09-30T15:00:00+08:00"
        self.assertEqual(confirmed_close_date(stamp,now,captured_at="2026-09-30T15:10:00+08:00"),"2026-09-30")
        for capture in (None,"2026-09-30T15:01:00+08:00","2026-09-30T15:10:00","2026-10-01T12:00:00+08:00"):
            self.assertIsNone(confirmed_close_date(stamp,now,captured_at=capture))
        self.assertIsNone(confirmed_close_date("2026-09-30T14:59:59+08:00",now,captured_at="2026-09-30T16:00:00+08:00"))

    def test_full_archive_is_idempotent_revisions_retained_on_change(self):
        share_current_flow_with_history(current(count=50),self.db)
        share_current_flow_with_history(current(count=50),Database(self.path))
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sector_capital_flow_daily").fetchone()[0],50)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM capital_flow_revisions").fetchone()[0],0)
        share_current_flow_with_history(current(value=-100,count=50),self.db)
        with self.db.connection() as conn:
            revisions = conn.execute("SELECT previous_json FROM capital_flow_revisions").fetchall()
        self.assertEqual(len(revisions),50)
        self.assertEqual(json.loads(revisions[0][0])["main_net_inflow"],100)

    def test_intraday_capture_does_not_later_become_close(self):
        early = current(capture="15:01:00")
        self.db.save_current_capital_flow(early)
        self.assertEqual(share_current_flow_with_history(self.db.get_current_capital_flow("concept"),self.db)["closed_rows_saved"],0)

    def test_daily_update_never_waits_for_history_and_display_limit_does_not_limit_storage(self):
        with patch("app.flow_archive.fetch_sector_capital_flow",return_value=current(count=60)) as live, patch("app.capital_flow._fetch_board_flow_history") as history:
            result = update_daily(self.db,"concept","2026-09-30")
        self.assertEqual(result["total"],60)
        self.assertFalse(result["window_complete"])
        self.assertEqual(result["inflow_days_rank"],[])
        live.assert_called_once_with("concept",1000); history.assert_not_called()

    def test_missing_middle_date_is_not_zero_and_older_date_not_substituted(self):
        days = trading_window(date(2026,9,30))
        for index, day in enumerate(days):
            report = current(day,100 if index < 6 else -1)
            if index == 3: report["rows"] = report["rows"][:1]
            share_current_flow_with_history(report,self.db)
        share_current_flow_with_history(current("2026-09-15"),self.db)
        report = fetch_sector_capital_flow_report("concept",50,"2026-09-30",self.db,local_only=True)
        self.assertEqual(report["history_coverage"]["complete_boards"],1)
        self.assertEqual([r["sector_code"] for r in report["inflow_days_rank"]],["BK0000"])
        self.assertEqual(report["inflow_days_rank"][0]["positive_flow_days"],6)
        self.assertEqual(report["window_dates"],days)
        sample = next(row for row in report["history_coverage"]["incomplete_samples"] if row["sector_code"]=="BK0001")
        self.assertEqual(sample["available_days"],9)

    def test_cache_clear_keeps_archives_revisions_and_keys(self):
        share_current_flow_with_history(current(),self.db)
        share_current_flow_with_history(current(value=200),self.db)
        self.db.save_current_capital_flow(current())
        result = self.db.clear_capital_flow_data(cache_only=True)
        self.assertEqual(result["deleted_rows"],1)
        self.assertIsNone(Database(self.path).get_current_capital_flow("concept"))
        self.assertEqual(len(self.db.get_sector_capital_flow_history("concept","2026-09-30")),2)
        with self.db.connection() as conn: self.assertEqual(conn.execute("SELECT COUNT(*) FROM capital_flow_revisions").fetchone()[0],2)

    def test_mirror_corrupt_file_rejected_next_identical_mirror_accepted(self):
        report = share_current_flow_with_history(current(),self.db)
        entry = export_daily(report,Path(self.temp.name)/"export",["https://primary.example","https://backup.example"])
        self.db.clear_capital_flow_data()
        payload = (Path(self.temp.name)/"export/2026-09-30/concept.json").read_bytes()
        index = json.dumps({"schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY","redistribution_authorized":True,"entries":[entry]}).encode()
        with patch("app.flow_archive._read",side_effect=[index,b"corrupt",payload]) as read:
            result = sync_feed(self.db,"concept","2026-09-30",["https://index.example/index.json"])
        self.assertEqual(read.call_count,3)
        self.assertEqual(result["saved_days"],["2026-09-30"])
        self.assertEqual(len(Database(self.path).get_sector_capital_flow_history("concept","2026-09-30")),2)
        with patch("app.flow_archive._read",return_value=index) as read:
            self.assertEqual(sync_feed(self.db,"concept","2026-09-30",["https://index.example"]) ["saved_days"],[])
        self.assertEqual(read.call_count,1)

    def test_bad_provider_date_units_duplicate_and_partial_are_atomic(self):
        good = {"schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY","sector_type":"concept","trade_date":"2026-09-30","closed":True,"captured_at":current()["captured_at"],"total":2,"rows":current()["rows"]}
        for key,value in (("source","ths"),("metric","net_amount"),("unit","亿元"),("trade_date","2026-09-29"),("total",3)):
            with self.assertRaises(ValueError): validate_daily({**good,key:value},"concept","2026-09-30")
        bad = copy.deepcopy(good); bad["rows"][1] = bad["rows"][0]
        with self.assertRaises(ValueError): validate_daily(bad,"concept","2026-09-30")
        bad = copy.deepcopy(good); bad["rows"][0]["main_net_inflow"] = float("nan")
        with self.assertRaises(ValueError): validate_daily(bad,"concept","2026-09-30")

    def test_unlicensed_feed_not_used_and_collection_failure_preserves_history(self):
        share_current_flow_with_history(current(),self.db)
        with patch("app.flow_archive._read",return_value=b'{"redistribution_authorized":false}'):
            result = sync_feed(self.db,"concept","2026-09-30",["https://feed.example"])
        self.assertTrue(result["errors"]); self.assertEqual(result["saved_days"],[])
        with patch("app.flow_archive.fetch_sector_capital_flow",side_effect=RuntimeError("offline")):
            result = update_daily(self.db,"concept","2026-09-30")
        self.assertEqual(result["total"],2); self.assertIn("offline",result["refresh_error"])

    def test_two_windows_timer_lease_and_successful_followup_are_bounded(self):
        a,b = DailyCollector(self.db),DailyCollector(Database(self.path))
        now = datetime.fromisoformat("2026-09-30T16:00:00+08:00")
        def response(database,sector_type):
            # Other window's tick during this lease must not collect concept.
            return {"archive_complete":True,"confirmed_trade_date":"2026-09-30","closed_rows_saved":500}
        with patch("app.flow_archive.collect_current",side_effect=response) as collect:
            a.tick(now); b.tick(now)
            self.assertEqual(collect.call_count,2)
            with self.db.connection() as conn: conn.execute("UPDATE capital_flow_collection SET next_attempt=0")
            b.tick(now)
            with self.db.connection() as conn: conn.execute("UPDATE capital_flow_collection SET next_attempt=0")
            a.tick(now)
            self.assertEqual(collect.call_count,4)

    def test_timer_respects_live_other_process_lease_and_clear_guard(self):
        with self.db.connection() as conn:
            conn.execute("INSERT INTO capital_flow_collection VALUES('concept','2026-09-30','{}','other',?,0)",(time.time()+120,))
        with self.assertRaises(RuntimeError): self.db.clear_capital_flow_data(cache_only=True)
        with patch("app.flow_archive.collect_current",return_value={}) as collect:
            DailyCollector(self.db).tick(datetime.fromisoformat("2026-09-30T16:00:00+08:00"))
            self.assertEqual(collect.call_count,1); self.assertEqual(collect.call_args.args[1],"industry")

    def test_daily_job_finishes_without_history_request(self):
        manager = FlowHistoryJobs()
        with patch("app.flow_archive.fetch_sector_capital_flow",return_value=current()), patch("app.capital_flow._fetch_board_flow_history") as hist:
            job = manager.start(self.db,"concept","2026-09-30",lambda fn:fn,mode="daily")
            for _ in range(200):
                if not manager.is_running(): break
                time.sleep(.01)
            done = manager.snapshot(job["id"])
        self.assertEqual(done["status"],"complete"); self.assertEqual(done["result"]["total"],2)
        self.assertFalse(done["result"]["window_complete"]); hist.assert_not_called()
        manager.shutdown()

    def test_full_current_paginates_instead_of_falsely_complete(self):
        def res(data):
            r = Mock(); r.json.return_value={"data":data}; return r
        page1 = {"total":3,"diff":[{"f12":"BK0001","f14":"一","f62":10}]}
        page2 = {"total":3,"diff":[{"f12":"BK0002","f14":"二","f62":20},{"f12":"BK0003","f14":"三","f62":30}]}
        with patch("app.capital_flow._EASTMONEY_DATA_HOSTS",()),patch("app.capital_flow._paced_flow_get",side_effect=[res(page1),res(page2)]) as req:
            result = fetch_sector_capital_flow("concept",1000)
        self.assertEqual(result["total"],3); self.assertEqual(len(result["rows"]),3)
        self.assertTrue(result["universe_complete"]); self.assertEqual(req.call_args.kwargs["params"]["pn"],"2")

    def test_timer_does_not_network_on_holiday_or_intraday(self):
        with patch("app.flow_archive.collect_current") as collect:
            for value in ("2026-10-06T16:00:00+08:00","2026-09-30T14:00:00+08:00"):
                DailyCollector(self.db).tick(datetime.fromisoformat(value))
        collect.assert_not_called()

    def test_old_database_upgrade_adds_tables_without_losing_daily_records(self):
        share_current_flow_with_history(current(),self.db)
        with self.db.connection() as conn:
            conn.execute("DROP TABLE capital_flow_collection")
            conn.execute("DROP TABLE capital_flow_revisions")
        upgraded = Database(self.path)
        self.assertEqual(len(upgraded.get_sector_capital_flow_history("concept","2026-09-30")),2)
        share_current_flow_with_history(current(value=200),upgraded)
        with upgraded.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM capital_flow_revisions").fetchone()[0],2)


if __name__ == "__main__": unittest.main()
